"""What the recorder was told to do, resolved once and frozen for the recording.

Why immutable
-------------
A recording is only interpretable if every event in it was produced under one set
of rules. If scope or redaction could change mid-recording, half the timeline
would be filtered one way and half another, and no reader -- human or the
reconstruct layer -- could tell which. So the config is a frozen dataclass:
resolved before `start()`, never mutated while recording.

Why the recorder does not import this module
--------------------------------------------
`RecorderConfig` is application-level configuration; the recorder is the bottom
layer and must stay ignorant of how it was configured. So this module (and the
CLI) build the recorder's collaborators -- a `Scope` and a `Redactor` -- from the
config and inject them. The dependency points down (config -> recorder), never up.

Precedence: CLI > env > file > default
--------------------------------------
Most specific wins. A flag on the command line is the most deliberate act, so it
overrides an environment variable, which overrides `[tool.chronotrace]` in
`pyproject.toml`, which overrides the built-in defaults. Each layer contributes
only the keys it actually sets, so unset keys fall through to the next layer down.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chronotrace.recorder.redact import DEFAULT_PATTERNS


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    """Immutable recording settings. See the module docstring for precedence.

    Attributes:
        roots: directories treated as "my code". Empty means "decide at record
            time" -- the CLI defaults it to the target script's directory.
        include: globs forcing files into scope (e.g. a dependency to debug into).
        exclude: globs forcing files out of scope.
        redact: name globs whose values are withheld (see `recorder.redact`).
        capture_values: record local values, not just control flow.
    """

    roots: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    redact: tuple[str, ...] = DEFAULT_PATTERNS
    capture_values: bool = True


_ENV_MAP = {
    "roots": "CHRONOTRACE_ROOTS",
    "include": "CHRONOTRACE_INCLUDE",
    "exclude": "CHRONOTRACE_EXCLUDE",
    "redact": "CHRONOTRACE_REDACT",
    "capture_values": "CHRONOTRACE_CAPTURE_VALUES",
}


def load_config(
    pyproject: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    cli: Mapping[str, Any] | None = None,
) -> RecorderConfig:
    """Resolve a config from the three override layers over the defaults.

    Args:
        pyproject: path to a `pyproject.toml`, or None to skip the file layer.
        env: an environment mapping, or None to skip the env layer. The CLI passes
            `os.environ`; tests pass an explicit dict, so this stays pure.
        cli: parsed CLI overrides (only keys the user gave), or None.

    Returns:
        A frozen `RecorderConfig` with CLI > env > file > default precedence.

    Complexity: O(fields).
    """
    layers = (_from_file(pyproject), _from_env(env), _from_cli(cli))  # low -> high

    def pick(key: str, default: Any) -> Any:
        for layer in reversed(layers):
            if key in layer:
                return layer[key]
        return default

    return RecorderConfig(
        roots=tuple(pick("roots", ())),
        include=tuple(pick("include", ())),
        exclude=tuple(pick("exclude", ())),
        redact=tuple(pick("redact", DEFAULT_PATTERNS)),
        capture_values=bool(pick("capture_values", True)),
    )


def _from_file(pyproject: str | Path | None) -> dict[str, Any]:
    if pyproject is None:
        return {}
    path = Path(pyproject)
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    table = data.get("tool", {}).get("chronotrace", {})
    return {k: v for k, v in table.items() if k in _ENV_MAP}


def _from_env(env: Mapping[str, str] | None) -> dict[str, Any]:
    if env is None:
        return {}
    out: dict[str, Any] = {}
    for field, var in _ENV_MAP.items():
        if var not in env:
            continue
        raw = env[var]
        out[field] = _parse_bool(raw) if field == "capture_values" else _split(raw)
    return out


def _from_cli(cli: Mapping[str, Any] | None) -> dict[str, Any]:
    """CLI overrides, dropping keys the user did not set (None / empty list)."""
    if cli is None:
        return {}
    return {k: v for k, v in cli.items() if v is not None and v != []}


def _split(raw: str) -> tuple[str, ...]:
    """Comma-separated env list -> tuple, trimmed, empties dropped."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def find_pyproject(start: str | Path | None = None) -> Path | None:
    """The nearest `pyproject.toml` at or above `start` (default cwd), or None.

    Complexity: O(depth of the directory tree).
    """
    here = Path(start).resolve() if start is not None else Path.cwd()
    for parent in (here, *here.parents):
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None
