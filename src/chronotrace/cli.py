"""The `chronotrace` command: record a script, step through it, repair a crashed recording.

`chronotrace record script.py [args...]` executes the target as `__main__` with
recording on, scoped by default to the script's own directory. `chronotrace step
script.py` records it and drops into the stepping REPL -- forward *and* backward --
which is the product in its smallest usable form until the UI lands. `chronotrace repair
rec.chrono` rebuilds the footer of a recording whose writer was killed mid-write, so
later opens are O(1) again -- and reports the truncation, because a recovered recording
is incomplete and the user must never be told otherwise.

Stdlib `argparse`, not a CLI framework: a handful of flags do not justify a dependency
the recorder's process would then carry (see the zero-deps note in pyproject.toml).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import runpy
import sys
from collections.abc import Iterable
from pathlib import Path

from chronotrace.config import RecorderConfig, find_pyproject, load_config
from chronotrace.index import Progress, build_index
from chronotrace.query import (
    PAGE_SIZE,
    CallersOfQuery,
    CallTreeQuery,
    Cursor,
    ExceptionOriginQuery,
    Hit,
    LastWriteBeforeQuery,
    LineHitsQuery,
    Query,
    QueryContext,
    QueryError,
    QueryResult,
    ValueProvenanceQuery,
    VarWritesQuery,
    registry,
)
from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.redact import Redactor
from chronotrace.recorder.scope import Scope
from chronotrace.repl import Repl
from chronotrace.store import ChronoError, ChronoReader, ChronoWriter, Strings, repair
from chronotrace.store.strings import CodeInfo


def build_parser() -> argparse.ArgumentParser:
    """The argument parser. Subcommands leave room for `serve` (day 33+)."""
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
    rec.add_argument(
        "--out",
        metavar="FILE",
        help="write the recording here (default: <script>.chrono beside the script)",
    )
    rec.add_argument(
        "--index",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="build the query index after recording (default: on)",
    )
    rec.add_argument("script", help="the Python script to record")
    rec.add_argument("script_args", nargs=argparse.REMAINDER, help="arguments passed to the script")

    stp = sub.add_parser("step", help="step through an execution, forward and backward")
    stp.add_argument("target", help="a .py script to record and step, or a .chrono recording")
    stp.add_argument("script_args", nargs=argparse.REMAINDER, help="arguments passed to the script")

    idx = sub.add_parser("index", help="build the query index for a recording")
    idx.add_argument("file", help="the .chrono file to index")

    qry = sub.add_parser("query", help="ask the recording a question, get jumpable instants")
    qry.add_argument("file", nargs="?", help="the .chrono recording to query")
    qry.add_argument("--list", action="store_true", help="list the available queries and exit")
    qry.add_argument("--var-writes", metavar="NAME", help="every write to variable NAME")
    qry.add_argument("--line-hits", metavar="FILE:LINE", help="every instant FILE:LINE executed")
    qry.add_argument("--last-write", metavar="NAME@SEQ", help="the last write to NAME before SEQ")
    qry.add_argument("--provenance", metavar="NAME@SEQ", help="where NAME's value at SEQ came from")
    qry.add_argument(
        "--exception-origin", type=int, metavar="SEQ", help="where the exception at SEQ was born"
    )
    qry.add_argument("--callers-of", metavar="FUNC", help="every invocation of function FUNC")
    qry.add_argument("--call-tree", type=int, metavar="FRAME", help="the direct children of FRAME")
    qry.add_argument("--frame", type=int, metavar="ID", help="scope --var-writes to one frame")
    qry.add_argument("--before", type=int, metavar="SEQ", help="only writes strictly before SEQ")
    qry.add_argument("--after", type=int, metavar="SEQ", help="resume paging after this instant")
    qry.add_argument("--limit", type=int, metavar="N", help="rows per page (default: 100)")

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
) -> Recorder:
    """Execute `script` as `__main__` under the recorder, into `sink`.

    Scope defaults to the script's own directory when the config names no roots,
    which is what a developer means by "my code". `sys.argv` is swapped so the
    target sees its own name and arguments, then restored.

    Args:
        script: path to the target script.
        script_args: arguments to expose to the target as `sys.argv[1:]`.
        config: resolved, immutable recording settings.
        sink: where events are written.

    Returns:
        The recorder, for its intern tables and value pool -- what `intern_tables` turns
        into the recording's own STRINGS section.

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
    return recorder


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


def step_command(args: argparse.Namespace) -> int:
    """Open a stepping session on a `.chrono` file, or on a script recorded right now.

    Both forms render real names: since format 1.6 the recording carries its own intern
    tables, so a `.chrono` opened days later shows exactly what the session that made it
    would have. Recording the script here still writes a real recording to memory rather
    than shortcutting past the format, so the demo exercises writer, reader and
    reconstruction end to end.
    """
    target = Path(args.target)
    if target.suffix == ".chrono":
        try:
            with ChronoReader.open(target) as reader:
                strings = reader.strings()
                Repl(
                    reader, names=dict(enumerate(strings.names)), codes=_code_labels(strings)
                ).run()
        except ChronoError as exc:
            print(f"chronotrace: cannot read {target}: {exc}", file=sys.stderr)  # noqa: T201
            return 1
        return 0

    config = load_config(pyproject=find_pyproject(), env=os.environ, cli={})
    sink = MemorySink()
    recorder = record_script(str(target), args.script_args, config, sink)
    if not sink.events:
        print(  # noqa: T201
            "chronotrace: nothing was recorded -- the scope may exclude all of your code.",
            file=sys.stderr,
        )
        return 1
    with ChronoReader.from_bytes(_to_chrono(recorder, sink)) as reader:
        strings = reader.strings()
        Repl(reader, names=dict(enumerate(strings.names)), codes=_code_labels(strings)).run()
    return 0


def intern_tables(recorder: Recorder) -> Strings:
    """The recorder's intern tables as plain data the store can persist.

    Lives here, above both layers, because `store` must not import the recorder's
    `InternTable` and the recorder must not know a file format exists. The CLI is the
    only place that legitimately knows about both.
    """
    return Strings(
        names=tuple(recorder.names),
        exc_types=tuple(recorder.exc_types),
        codes=tuple(
            CodeInfo(code.co_filename, code.co_qualname, code.co_firstlineno)
            for code in recorder.codes
        ),
        source_hashes=_source_hashes(code.co_filename for code in recorder.codes),
    )


def _source_hashes(filenames: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """SHA-256 of each unique, readable recorded source file -- for provenance verification.

    Hashed here, at write time in the same run that recorded, so the digest is of the
    source *as it ran*. A file that cannot be read now (`<string>` from `exec`, a deleted
    temp, a C-backed module) is omitted rather than faked -- no entry means a later query
    reads it as "cannot verify", never as "verified". The recorder itself never does this
    I/O; it belongs above the hot path.
    """
    digests: dict[str, str] = {}
    for filename in filenames:
        if filename in digests:
            continue
        try:
            digests[filename] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        except OSError:
            continue  # unreadable source: omit, so the query cannot mistake it for verified
    return tuple(digests.items())


def _to_chrono(recorder: Recorder, sink: MemorySink) -> bytes:
    """Write a finished in-memory recording to `.chrono` bytes.

    Values go in first and in reference order, so the pool's own content-addressed
    numbering lands on the `value_ref`s the events already cite.
    """
    buf = io.BytesIO()
    writer = ChronoWriter(buf)
    writer.add_strings(intern_tables(recorder))
    for captured in recorder.values:
        writer.add_value(captured)
    for event in sink.events:
        writer.add(event)
    writer.close()
    return buf.getvalue()


def _code_labels(strings: Strings) -> dict[int, str]:
    """`code_id` -> "qualname (filename)", the shortest text that identifies a frame.

    Reads the *recording's* tables rather than a live recorder, so a `.chrono` opened
    days later renders exactly what the session that made it would have (format 1.6).
    """
    return {i: f"{c.qualname} ({Path(c.filename).name})" for i, c in enumerate(strings.codes)}


def index_command(path: str) -> int:
    """Build the sidecar index for a recording. Returns an exit code.

    Idempotent: an existing index is replaced atomically, so running it twice is safe and
    running it on a recording that already has a current index simply rebuilds one.
    """
    recording = Path(path)
    try:
        with ChronoReader.open(recording) as reader:
            result = build_index(recording, reader, on_progress=_report_progress)
    except ChronoError as exc:
        print(f"chronotrace: cannot read {recording}: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    partial = " (partial -- the recording is crash-truncated)" if result.partial else ""
    print(  # noqa: T201
        f"chronotrace: indexed {result.events:,} events into {result.rows:,} rows "
        f"-> {result.path}{partial}"
    )
    return 0


def _report_progress(progress: Progress) -> None:
    """Report indexing progress on a single rewritten line.

    A large recording is a visible wait, and a wait with no feedback is indistinguishable
    from a hang -- which is how a tool gets uninstalled.
    """
    print(f"\rchronotrace: indexing {progress.fraction:5.0%}", end="", file=sys.stderr)  # noqa: T201


def query_command(args: argparse.Namespace) -> int:
    """Run one query against a recording, printing jump-to-`seq` results. Returns an exit code.

    The index is built on demand if it is missing (`QueryContext.open`), so a first query on
    a bare recording just works -- it waits, once, rather than failing with "run index
    first". `--list` needs no recording; every other form needs exactly one query flag.
    """
    if args.list:
        for name, summary in registry.summaries().items():
            print(f"  {name:12s} {summary}")  # noqa: T201
        return 0
    if not args.file:
        print("chronotrace: query needs a .chrono file, or --list", file=sys.stderr)  # noqa: T201
        return 2
    try:
        query = _build_query(args)
    except ValueError as exc:
        print(f"chronotrace: {exc}", file=sys.stderr)  # noqa: T201
        return 2

    recording = Path(args.file)
    cursor = Cursor(args.after) if args.after is not None else None
    try:
        with QueryContext.open(recording, on_progress=_report_progress) as ctx:
            result = query.execute(ctx, cursor, limit=args.limit or PAGE_SIZE)
    except ChronoError as exc:
        print(f"chronotrace: cannot read {recording}: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    except QueryError as exc:
        print(f"chronotrace: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    _render(result, _empty_note(args))
    return 0


def _build_query(args: argparse.Namespace) -> Query:
    """Turn the CLI flags into a typed query. Exactly one query flag, or it is an error.

    This is where the typed API pays off: each flag maps to a constructor call, and a
    composed query (origin then provenance) is a call, not a parse. A DSL would have made
    this a grammar; here it is a dispatch (see the day-28 no-DSL decision, #13).
    """
    given = {
        name: value
        for name, value in (
            ("--var-writes", args.var_writes),
            ("--line-hits", args.line_hits),
            ("--last-write", args.last_write),
            ("--provenance", args.provenance),
            ("--exception-origin", args.exception_origin),
            ("--callers-of", args.callers_of),
            ("--call-tree", args.call_tree),
        )
        if value is not None
    }
    if len(given) != 1:
        raise ValueError("give exactly one query flag (e.g. --var-writes, --provenance)")
    if args.var_writes is not None:
        return VarWritesQuery(name=args.var_writes, frame_id=args.frame, before_seq=args.before)
    if args.line_hits is not None:
        file, sep, line = args.line_hits.rpartition(":")
        if not sep or not line.isdigit():
            raise ValueError(
                f"--line-hits wants FILE:LINE, e.g. app.py:42 (got {args.line_hits!r})"
            )
        return LineHitsQuery(file=file, lineno=int(line))
    if args.last_write is not None:
        name, seq = _name_at_seq("--last-write", args.last_write)
        return LastWriteBeforeQuery(name=name, seq=seq, frame_id=args.frame)
    if args.provenance is not None:
        name, seq = _name_at_seq("--provenance", args.provenance)
        return ValueProvenanceQuery(name=name, seq=seq)
    if args.exception_origin is not None:
        return ExceptionOriginQuery(seq=args.exception_origin)
    if args.callers_of is not None:
        return CallersOfQuery(function=args.callers_of)
    return CallTreeQuery(frame_id=args.call_tree)


def _name_at_seq(flag: str, value: str) -> tuple[str, int]:
    """Parse a `NAME@SEQ` argument, or explain the shape. Splits on the last `@`."""
    name, sep, seq = value.rpartition("@")
    if not sep or not seq.lstrip("-").isdigit():
        raise ValueError(f"{flag} wants NAME@SEQ, e.g. total@1500 (got {value!r})")
    return name, int(seq)


def _render(result: QueryResult, empty_note: str) -> None:
    """Print a page of hits, then the two things a page must not hide: partial and 'more'."""
    if result.hits:
        for hit in result.hits:
            print(_format_hit(hit))  # noqa: T201
    else:
        print(empty_note)  # noqa: T201
    if result.partial:
        print(  # noqa: T201
            "chronotrace: results are PARTIAL -- the recording is crash-truncated, so "
            "instants past the truncation cannot be found.",
            file=sys.stderr,
        )
    if result.next_cursor is not None:
        print(  # noqa: T201
            f"chronotrace: more results -- rerun with --after {result.next_cursor.after_seq}",
            file=sys.stderr,
        )


def _format_hit(hit: Hit) -> str:
    """One result line, led by its `[seq]` -- the address the UI will make clickable."""
    parts = [f"[{hit.seq}]"]
    if hit.file is not None:
        where = Path(hit.file).name
        parts.append(f"{where}:{hit.lineno}" if hit.lineno is not None else where)
    if hit.function is not None:
        parts.append(hit.function)
    if hit.value_preview is not None:
        parts.append(f"= {hit.value_preview}")
    line = "  ".join(parts)
    return f"{line}   -- {hit.note}" if hit.note is not None else line


def _empty_note(args: argparse.Namespace) -> str:
    """The right 'found nothing' message for the query that ran -- they are not the same.

    Each query's empty result means something specific, and a shared "0 results" would send
    a user chasing the wrong thing. An unrecorded origin is not a bug in their query; a line
    that never ran is not the same as one that does not exist.
    """
    if args.var_writes is not None:
        return (
            f"chronotrace: {args.var_writes!r} exists but has no writes in the range you asked "
            "for (try widening --before, or dropping --frame)."
        )
    if args.line_hits is not None:
        return (
            f"chronotrace: {args.line_hits} never executed -- or the line is blank, a comment, or "
            "past the end of the file, which the index cannot distinguish without the source."
        )
    if args.last_write is not None:
        return (
            f"chronotrace: no write to {args.last_write} was recorded"
            " -- nothing set it before then."
        )
    if args.provenance is not None:
        return f"chronotrace: no write to {args.provenance} was recorded -- nothing to trace."
    if args.exception_origin is not None:
        return (
            f"chronotrace: no exception with a recorded origin is visible at seq "
            f"{args.exception_origin} -- it may have been raised in code ChronoTrace did not "
            "record (the stdlib, or a C extension)."
        )
    if args.callers_of is not None:
        return (
            f"chronotrace: {args.callers_of!r} was recorded but not called"
            " in the range you asked for."
        )
    return (
        f"chronotrace: frame {args.call_tree} has no recorded children -- a leaf call, or the "
        "frame id is not one this recording assigned."
    )


def record_command(args: argparse.Namespace) -> int:
    """Record the target script to a `.chrono` file and index it."""
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
    recorder = record_script(args.script, args.script_args, config, sink)

    count = len(sink.events)
    if count == 0:
        print(  # noqa: T201
            "chronotrace: warning -- nothing was recorded. The scope may exclude "
            "all of your code; try --include.",
            file=sys.stderr,
        )
        return 0
    out = Path(args.out) if args.out else Path(args.script).with_suffix(".chrono")
    out.write_bytes(_to_chrono(recorder, sink))
    print(f"chronotrace: recorded {count:,} events from {args.script} -> {out}")  # noqa: T201
    return index_command(str(out)) if args.index else 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the chosen subcommand. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "repair":
        return repair_recording(args.file, args.out)
    if args.command == "step":
        return step_command(args)
    if args.command == "index":
        return index_command(args.file)
    if args.command == "query":
        return query_command(args)
    return record_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
