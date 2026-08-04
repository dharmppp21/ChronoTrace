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
