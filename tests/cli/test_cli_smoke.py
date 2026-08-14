"""CLI smoke tests: the console script's contract, and the lazy-import guarantee.

These exist because day 44 moved every engine import out of `cli.py`'s module scope to keep
`--help`/`--version` fast. That refactor is exactly the kind that a NameError finds at runtime,
not at import -- so the whole dispatch is exercised here, and the lazy property is asserted
structurally (no heavy module in `sys.modules` after importing the CLI), which cannot flake on
timing the way a wall-clock budget would.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from chronotrace.cli import main

# Engine modules that `chronotrace --help` must NOT pull in. store drags zstandard/msgpack (C
# extensions); fastapi/uvicorn are the [ui] extra a recorder-only user never installs.
HEAVY = ("chronotrace.store", "chronotrace.query", "chronotrace.index", "fastapi", "uvicorn")


def test_version_reports_package_format_and_ui(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert "chronotrace" in out
    assert ".chrono format" in out
    assert "[ui] extra:" in out  # the three things a bug report needs, per the brief


def test_no_command_prints_help_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2  # nothing to do, but not a crash -- usage on stderr
    assert "usage" in capsys.readouterr().err.lower()


def test_ui_landing_page_follows_whether_the_scrubber_is_bundled() -> None:
    """`record --ui` must open the scrubber, not the API explorer.

    It opened `/docs` for a long time -- correct while the UI did not exist yet, and quietly
    wrong from the day it landed, because nothing failed: the browser opened, showed a list of
    endpoints, and the one-command "watch it record" promise silently delivered the wrong page.
    A stale fallback that still *works* is exactly the kind that survives.

    The assertion is that the CLI agrees with the server about what is installed, since
    `server.app._mount_ui` serves the SPA at `/` under the same condition.
    """
    from chronotrace.cli import _has_bundled_ui
    from chronotrace.server.app import _UI_DIR

    assert _has_bundled_ui() == (_UI_DIR / "index.html").is_file()


def test_importing_the_cli_does_not_pull_the_engine() -> None:
    # Fresh subprocess so sys.modules is clean of what other tests have imported.
    probe = (
        "import chronotrace.cli, sys;"
        f"leaked=[m for m in {HEAVY!r} if m in sys.modules];"
        "assert not leaked, leaked"
    )
    result = subprocess.run(  # noqa: S603 -- sys.executable, fixed probe, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_cli_import_stays_under_budget() -> None:
    # 8x margin over the measured ~40ms so it catches re-adding a heavy eager import (fastapi
    # alone is ~400ms) without flaking on a loaded runner. Import time, not subprocess wall,
    # because interpreter startup is machine noise we do not control.
    probe = "import time;t=time.perf_counter();import chronotrace.cli;print(time.perf_counter()-t)"
    result = subprocess.run(  # noqa: S603 -- sys.executable, fixed probe, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout) < 0.3, f"cli import took {result.stdout.strip()}s"


def test_record_then_query_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "bug.py"
    script.write_text(
        "def run(n):\n"
        "    total = 0\n"
        "    for i in range(n):\n"
        "        total += i\n"
        "    return total\n"
        "run(5)\n"
    )
    chrono = tmp_path / "bug.chrono"
    # Flags before the positional script: REMAINDER eats everything after it (a known CLI gotcha).
    assert main(["record", "--out", str(chrono), "--no-index", str(script)]) == 0
    assert chrono.exists()
    capsys.readouterr()  # drop the record output

    assert main(["query", str(chrono), "--line-hits", f"{script}:4"]) == 0
    out = capsys.readouterr().out
    assert "bug.py:4" in out  # the accumulator line executed; the query found its instants


def test_a_crashing_script_still_leaves_a_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run you most need to scrub is the one that died.

    The target's exception used to propagate out of `record_script`, so the write never
    happened and the recording was discarded at exactly the moment it mattered -- the primary
    use case of a time-travel debugger, losing its data.
    """
    script = tmp_path / "boom.py"
    script.write_text("def f(x):\n    return int(x)\nf('')\n")
    monkeypatch.chdir(tmp_path)

    assert main(["record", str(script)]) == 1, "a crashed target is a non-zero exit"
    chrono = tmp_path / "boom.chrono"
    assert chrono.exists() and chrono.stat().st_size > 0, "the recording must survive the crash"

    err = capsys.readouterr().err
    assert "ValueError" in err  # the crash is reported, not swallowed

    # And the survivor is actually usable, not just present.
    assert main(["query", "boom.chrono", "--watch", "x"]) == 0


def test_a_mistyped_script_path_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A wrong path is a wrong path, not a scope problem.

    It used to run the whole machinery and report 'nothing was recorded -- try --include',
    which sends someone to debug their scope configuration over a typo.
    """
    assert main(["record", str(tmp_path / "does_not_exist.py")]) == 2
    err = capsys.readouterr().err
    assert "no such script" in err
    assert "--include" not in err  # the old, misleading advice


def test_a_failure_before_any_event_is_reported_not_blamed_on_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A syntax error records zero events -- and must still surface, not read as a scope issue.

    Capturing the target's exception (so crashed runs keep their recording) made the
    zero-event branch swallow it: the user saw 'try --include' where a SyntaxError belonged.
    """
    script = tmp_path / "bad.py"
    script.write_text("def broken(:\n    pass\n")
    monkeypatch.chdir(tmp_path)

    assert main(["record", str(script)]) == 1
    err = capsys.readouterr().err
    assert "SyntaxError" in err
    assert "--include" not in err


def test_a_crash_reports_where_the_exception_was_born(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recording a program that dies should SHOW the origin, not print a command to go run.

    Python's traceback already says where the exception surfaced. What the recording uniquely
    knows is where it was born -- the deepest frame, which for a chained exception can be far
    from the reported one -- so the whole point is answering that without being asked.
    """
    script = tmp_path / "deep.py"
    script.write_text(
        "def inner(q):\n"
        "    return int(q)\n"
        "def outer(rows):\n"
        "    return [inner(r) for r in rows]\n"
        "outer(['1', ''])\n"
    )
    monkeypatch.chdir(tmp_path)

    assert main(["record", str(script)]) == 1
    err = capsys.readouterr().err
    assert "where it came from" in err
    assert "born here" in err, err
    assert "deep.py" in err
    assert "chronotrace step" in err  # the next move, not a query to retype


def test_query_defaults_to_the_newest_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`chronotrace query --watch x` with no path: the recording you just made is the one meant.

    Naming the .chrono on every question was pure ceremony in the common record-then-ask loop.
    """
    script = tmp_path / "bug.py"
    script.write_text(
        "def run(n):\n    total = 0\n    for i in range(n):\n        total += i\nrun(3)\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["record", str(script)]) == 0
    capsys.readouterr()

    assert main(["query", "--watch", "total"]) == 0
    captured = capsys.readouterr()
    assert "total changed" in captured.out
    # Never a silent choice: an answer about the wrong recording would look just as authoritative.
    assert "using bug.chrono" in captured.err


def test_query_prefers_the_most_recent_of_several(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two recordings present: the newest wins, rather than refusing to choose.

    Refusing whenever a directory holds two would disable the shortcut in exactly the directory
    someone debugs in most.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "old.chrono").write_bytes(b"not a real recording")
    import os
    import time

    os.utime(tmp_path / "old.chrono", (time.time() - 3600, time.time() - 3600))
    script = tmp_path / "new.py"
    script.write_text("def run():\n    total = 1\n    return total\nrun()\n")
    assert main(["record", str(script)]) == 0
    capsys.readouterr()

    assert main(["query", "--watch", "total"]) == 0
    assert "using new.chrono" in capsys.readouterr().err


def test_query_without_a_recording_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty directory is a usage error with the next command in it, not a traceback."""
    monkeypatch.chdir(tmp_path)
    assert main(["query", "--watch", "total"]) == 2
    err = capsys.readouterr().err
    assert "no .chrono recording" in err
    assert "chronotrace record" in err  # tells them the very next thing to type
