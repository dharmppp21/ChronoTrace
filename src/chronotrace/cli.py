"""The `chronotrace` command: run a script under the recorder.

`chronotrace run script.py [args...]` executes the target as `__main__` with
recording on, scoped by default to the script's own directory. Today the events
go to an in-memory sink and the command reports a count; day 12 wires the
file-backed store so recordings persist.

Stdlib `argparse`, not a CLI framework: four flags and one positional do not
justify a dependency the recorder's process would then carry (see the zero-deps
note in pyproject.toml).
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from chronotrace.config import RecorderConfig, find_pyproject, load_config
from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.redact import Redactor
from chronotrace.recorder.scope import Scope


def build_parser() -> argparse.ArgumentParser:
    """The argument parser. A `run` subcommand leaves room for `replay` (day 30+)."""
    parser = argparse.ArgumentParser(prog="chronotrace", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="record a script's execution")
    run.add_argument("--include", action="append", metavar="GLOB", help="force a file into scope")
    run.add_argument("--exclude", action="append", metavar="GLOB", help="force a file out of scope")
    run.add_argument(
        "--redact", action="append", metavar="GLOB", help="redact locals matching this"
    )
    run.add_argument(
        "--capture-values",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="record local values, not just control flow (default: on)",
    )
    run.add_argument("script", help="the Python script to record")
    run.add_argument("script_args", nargs=argparse.REMAINDER, help="arguments passed to the script")
    return parser


def record_script(
    script: str, script_args: list[str], config: RecorderConfig, sink: MemorySink
) -> None:
    """Execute `script` as `__main__` under the recorder, into `sink`.

    Scope defaults to the script's own directory when the config names no roots,
    which is what a developer means by "my code". `sys.argv` is swapped so the
    target sees its own name and arguments, then restored.

    Args:
        script: path to the target script.
        script_args: arguments to expose to the target as `sys.argv[1:]`.
        config: resolved, immutable recording settings.
        sink: where events are written.

    Complexity: dominated by running the target program.
    """
    script_path = Path(script).resolve()
    roots = list(config.roots) or [str(script_path.parent)]
    recorder = Recorder(
        sink,
        scope=Scope(roots=roots, include=config.include, exclude=config.exclude),
        capture_values=config.capture_values,
        redact=Redactor(config.redact),
    )
    saved_argv = sys.argv
    sys.argv = [str(script_path), *script_args]
    try:
        with recorder:
            runpy.run_path(str(script_path), run_name="__main__")
    finally:
        sys.argv = saved_argv


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, record, and report. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    cli_overrides = {
        "include": args.include,
        "exclude": args.exclude,
        "redact": args.redact,
        "capture_values": args.capture_values,
    }
    config = load_config(pyproject=find_pyproject(), env=os.environ, cli=cli_overrides)

    sink = MemorySink()
    record_script(args.script, args.script_args, config, sink)

    count = len(sink.events)
    print(f"chronotrace: recorded {count} events from {args.script}")  # noqa: T201
    if count == 0:
        print(  # noqa: T201
            "chronotrace: warning -- nothing was recorded. The scope may exclude "
            "all of your code; try --include.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
