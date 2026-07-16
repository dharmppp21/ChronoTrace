"""Config precedence (CLI > env > file > default), immutability, and the CLI path."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from chronotrace.cli import main, record_script
from chronotrace.config import RecorderConfig, find_pyproject, load_config
from chronotrace.recorder import MemorySink


def _pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[tool.chronotrace]\ninclude = ["file_inc"]\nexclude = ["file_exc"]\n',
        encoding="utf-8",
    )
    return path


def test_defaults_when_nothing_is_set() -> None:
    config = load_config()
    assert config.include == ()
    assert config.exclude == ()
    assert config.capture_values is True
    assert "*password*" in config.redact


def test_file_layer_is_read(tmp_path: Path) -> None:
    config = load_config(pyproject=_pyproject(tmp_path))
    assert config.include == ("file_inc",)
    assert config.exclude == ("file_exc",)


def test_env_beats_file(tmp_path: Path) -> None:
    config = load_config(pyproject=_pyproject(tmp_path), env={"CHRONOTRACE_INCLUDE": "env_inc"})
    assert config.include == ("env_inc",)
    assert config.exclude == ("file_exc",), "a key not set in env falls through to the file"


def test_cli_beats_env_beats_file(tmp_path: Path) -> None:
    config = load_config(
        pyproject=_pyproject(tmp_path),
        env={"CHRONOTRACE_INCLUDE": "env_inc"},
        cli={"include": ["cli_inc"]},
    )
    assert config.include == ("cli_inc",)
    assert config.exclude == ("file_exc",), "exclude, set only in the file, survives"


def test_cli_none_and_empty_do_not_override(tmp_path: Path) -> None:
    """argparse gives absent flags as None/[]; those must not clobber lower layers."""
    config = load_config(pyproject=_pyproject(tmp_path), cli={"include": None, "exclude": []})
    assert config.include == ("file_inc",)
    assert config.exclude == ("file_exc",)


def test_env_comma_list_and_bool_parsing() -> None:
    config = load_config(
        env={"CHRONOTRACE_EXCLUDE": "a, b ,c", "CHRONOTRACE_CAPTURE_VALUES": "false"}
    )
    assert config.exclude == ("a", "b", "c")
    assert config.capture_values is False


def test_config_is_frozen() -> None:
    """Immutable: a config that changed mid-recording would make it uninterpretable."""
    config = RecorderConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.include = ("x",)  # type: ignore[misc]


def test_find_pyproject_walks_up(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.chronotrace]\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_pyproject(nested) == tmp_path / "pyproject.toml"


def test_record_script_runs_and_records(tmp_path: Path) -> None:
    script = tmp_path / "app.py"
    script.write_text("def f(n):\n    return n + 1\n\nf(3)\n", encoding="utf-8")
    sink = MemorySink()
    record_script(str(script), [], RecorderConfig(roots=(str(tmp_path),)), sink)
    assert sink.events, "the target's own code should be recorded"


def test_record_script_redacts_end_to_end(tmp_path: Path, monkeypatch: Any) -> None:
    """The CLI path builds a redactor from config; the secret never reaches capture."""
    from chronotrace.recorder.capture import capture as real

    seen: list[object] = []

    def spy(value: object, *args: Any, **kwargs: Any) -> Any:
        seen.append(value)
        return real(value, *args, **kwargs)

    monkeypatch.setattr("chronotrace.recorder.recorder.capture", spy)

    script = tmp_path / "app.py"
    # A trailing line so `keep`'s value is observed at a LINE event after assignment.
    script.write_text("api_secret = 'xyz'  # noqa\nkeep = 7\nresult = keep + 1\n", encoding="utf-8")
    record_script(str(script), [], RecorderConfig(roots=(str(tmp_path),)), MemorySink())

    assert "xyz" not in seen, "secret leaked through the CLI recording path"
    assert 7 in seen


def test_main_returns_zero_and_reports(tmp_path: Path, capsys: Any) -> None:
    script = tmp_path / "app.py"
    script.write_text("x = 1 + 1\n", encoding="utf-8")
    assert main(["run", str(script)]) == 0
    assert "recorded" in capsys.readouterr().out
