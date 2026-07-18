"""The `chronotrace` command: record a script, and repair a crashed recording.

`chronotrace record script.py [args...]` executes the target as `__main__` with
recording on, scoped by default to the script's own directory. `chronotrace repair
rec.chrono` rebuilds the footer of a recording whose writer was killed mid-write, so
later opens are O(1) again -- and reports the truncation, because a recovered recording
is incomplete and the user must never be told otherwise.

Stdlib `argparse`, not a CLI framework: a handful of flags do not justify a dependency
the recorder's process would then carry (see the zero-deps note in pyproject.toml).
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
from chronotrace.store import ChronoError, ChronoReader, repair


def build_parser() -> argparse.ArgumentParser:
    """The argument parser. A `record` subcommand leaves room for `replay` (day 30+)."""
    parser = argparse.ArgumentParser(prog="chronotrace", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="record a script's execution")
    rec.add_argument("--include", action="append", metavar="GLOB", help="force a file into scope")
    rec.add_argument("--exclude", action="append", metavar="GLOB", help="force a file out of scope")
    rec.add_argument(
        "--redact", action="append", metavar="GLOB", help="redact locals matching this"
    )
    rec.add_argument(
        "--capture-values",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="record local values, not just control flow (default: on)",
    )
    rec.add_argument("script", help="the Python script to record")
    rec.add_argument("script_args", nargs=argparse.REMAINDER, help="arguments passed to the script")

    rep = sub.add_parser("repair", help="rebuild the footer of a crash-truncated recording")
    rep.add_argument("file", help="the .chrono file to repair")
    rep.add_argument(
        "--out",
        metavar="FILE",
        help="write the repaired copy here; default swaps the original in place, atomically",
    )
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


def repair_recording(path: str, out: str | None) -> int:
    """Report a recording's truncation and rebuild its footer. Returns an exit code.

    Opens the recording first (recovering a crashed prefix), so the count and truncation
    it reports are the real recovered ones. A recording that is already intact is left
    untouched -- `repair` is idempotent.
    """
    src = Path(path)
    try:
        with ChronoReader.open(src) as reader:
            events, truncated = len(reader), reader.truncated
    except ChronoError as exc:
        print(f"chronotrace: cannot read {src}: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    if not truncated:
        print(f"chronotrace: {src} is intact ({events:,} events); nothing to repair.")  # noqa: T201
        return 0
    dst = repair(src, out)  # atomic; never modifies the original unless it IS the target
    print(  # noqa: T201
        f"chronotrace: recovered {events:,} events from a crash-truncated recording and "
        f"wrote a valid footer to {dst}. The recording is still incomplete -- the crash "
        f"lost its tail -- and stays flagged truncated."
    )
    return 0


def record_command(args: argparse.Namespace) -> int:
    """Record the target script into an in-memory sink and report the event count."""
    config = load_config(
        pyproject=find_pyproject(),
        env=os.environ,
        cli={
            "include": args.include,
            "exclude": args.exclude,
            "redact": args.redact,
            "capture_values": args.capture_values,
        },
    )
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


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the chosen subcommand. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "repair":
        return repair_recording(args.file, args.out)
    return record_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
