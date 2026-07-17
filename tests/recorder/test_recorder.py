"""Proves the recorder records what a human expects, and never harms the target.

The golden test below is the centre of gravity. Everything else in this file
protects the target program from us.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

import pytest

from chronotrace.recorder import Event, EventKind, MemorySink, Recorder
from chronotrace.recorder.scope import Scope

EXAMPLES = Path(__file__).parent.parent.parent / "examples"


def _record(fn: Any, *, capture_values: bool = False) -> tuple[MemorySink, Recorder]:
    """Record `fn`, control-flow only by default.

    Day 7 turned value capture on by default, which is right for users and wrong
    for this file: these tests pin the *control-flow* stream, and a VAR_WRITE per
    local per line would bury the handwritten golden sequence a human is supposed
    to be able to verify. tests/recorder/test_capture.py owns the value half.
    """
    sink = MemorySink()
    rec = Recorder(sink, capture_values=capture_values)
    with rec:
        fn()
    return sink, rec


def _stream(sink: MemorySink, rec: Recorder, *only: Any) -> list[tuple[str, int]]:
    """Events from `only`'s code objects, as (kind, lineno).

    Filtering is not cosmetic. The recorder correctly records *this test file*
    too -- `_record` calls `fn()` while monitoring is live, so the harness's own
    lines are real events from real user code. The first draft of the golden test
    below forgot that and failed, which is precisely what a handwritten
    expectation is for.

    Resolves `code_id` through the recorder's intern table. Reaching into a
    private attribute is deliberate: the alternative is exposing a code-resolution
    API on `Recorder` that nothing in production wants, purely so a test can read
    it. Day 12 needs the side table for real and will export it then.
    """
    wanted = {f.__code__ for f in only}
    return [(e.kind.name, e.lineno) for e in sink.events if rec._codes.resolve(e.code_id) in wanted]


def _events_of(sink: MemorySink, rec: Recorder, *only: Any) -> list[Event]:
    """Same filter as `_stream`, keeping the Event objects."""
    wanted = {f.__code__ for f in only}
    return [e for e in sink.events if rec._codes.resolve(e.code_id) in wanted]


def test_records_a_handwritten_expected_stream() -> None:
    """The golden test: a human wrote this sequence, then the recorder agreed.

    Uses a local function rather than examples/simple.py so the expected line
    numbers are visible three lines above the assertion. If this fails after an
    edit *here*, the expectation moved and must be re-derived by reading -- never
    by pasting whatever the recorder now emits, which would turn the referee into
    a rubber stamp.
    """

    def add_one(n: int) -> int:
        return n + 1  # line A

    def run() -> int:
        total = 0  # line B
        total += add_one(1)  # line C
        return total  # line D

    sink, rec = _record(run)
    stream = _stream(sink, rec, run, add_one)

    a = add_one.__code__.co_firstlineno + 1
    b = run.__code__.co_firstlineno + 1
    assert stream == [
        ("CALL", run.__code__.co_firstlineno),
        ("LINE", b),  # total = 0
        ("LINE", b + 1),  # total += add_one(1)
        ("CALL", add_one.__code__.co_firstlineno),
        ("LINE", a),  # return n + 1
        ("RETURN", add_one.__code__.co_firstlineno),
        ("LINE", b + 2),  # return total
        ("RETURN", run.__code__.co_firstlineno),
    ]


def test_records_the_example_program() -> None:
    """examples/simple.py end to end: the demo fixture actually records."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        import simple  # type: ignore[import-not-found]

        sink, _ = _record(simple.main)
    finally:
        sys.path.remove(str(EXAMPLES))

    kinds = [e.kind for e in sink.events]
    assert kinds.count(EventKind.CALL) == 7, "main + 2x quadruple + 4x double"
    assert kinds.count(EventKind.CALL) == kinds.count(EventKind.RETURN)
    assert EventKind.LINE in kinds


def test_seq_is_dense_and_increasing() -> None:
    """Across the whole stream, including the harness's own recorded lines."""
    sink, _ = _record(lambda: sum(range(3)))
    seqs = [e.seq for e in sink.events]
    assert seqs == sorted(seqs)
    assert seqs == list(range(len(seqs)))


def test_frame_ids_are_unique_among_live_frames_under_recursion() -> None:
    """Recursion: one code object, many live frames. frame_id is per-frame.

    Keying on the code object -- or on `id(frame)`, which CPython reuses -- would
    fuse every recursive call into one node and make the call tree a lie.
    """

    def countdown(n: int) -> int:
        if n <= 0:
            return 0
        return countdown(n - 1)

    sink, rec = _record(lambda: countdown(5))

    live: list[int] = []
    for e in _events_of(sink, rec, countdown):
        if e.kind is EventKind.CALL:
            assert e.frame_id not in live, "frame_id reused while still live"
            live.append(e.frame_id)
        elif e.kind is EventKind.RETURN:
            live.remove(e.frame_id)
    assert live == [], "every frame that entered must exit"


def test_frame_stack_is_balanced() -> None:
    def nested() -> int:
        def inner() -> int:
            return 1

        return inner() + inner()

    sink, rec = _record(nested)
    depth = 0
    for e in _events_of(sink, rec, nested):
        if e.kind is EventKind.CALL:
            depth += 1
        elif e.kind is EventKind.RETURN:
            depth -= 1
        assert depth >= 0, "RETURN without CALL"
    assert depth == 0


def test_never_records_itself() -> None:
    """Self-recording is infinite regress. Zero events from chronotrace/.

    Resolves every event's code object to a real filename. An earlier draft
    asserted against `str(event)`, which is the dataclass repr and contains no
    filename at all -- so it passed while checking nothing. A test that cannot
    fail is worse than no test, because it buys false confidence.
    """
    sink, rec = _record(lambda: sum(range(5)))
    assert sink.events, "sanity: something was recorded"

    import chronotrace

    package_root = str(Path(chronotrace.__file__).parent)
    offenders = [
        rec._codes.resolve(e.code_id).co_filename
        for e in sink.events
        if rec._codes.resolve(e.code_id).co_filename.startswith(package_root)
    ]
    assert offenders == [], f"recorded our own code: {set(offenders)}"


def test_stop_is_idempotent() -> None:
    rec = Recorder(MemorySink())
    rec.start()
    rec.stop()
    rec.stop()


@pytest.mark.parametrize("boom", [None, ValueError, SystemExit, KeyboardInterrupt])
def test_stop_releases_the_tool_id_on_every_exit_path(boom: type[BaseException] | None) -> None:
    """A leaked tool id is unrecoverable: six exist, and the next run cannot attach.

    stop() must run on every way out of the `with` block -- a clean return, a
    normal exception, and the two BaseExceptions a real program throws at us:
    SystemExit (sys.exit) and KeyboardInterrupt (Ctrl-C). This is exactly why
    __exit__ catches BaseException rather than Exception, and why try/finally in
    stop() is not optional. Proven by starting a second recorder afterwards: if the
    id leaked, it cannot attach.
    """
    rec = Recorder(MemorySink())
    guard = contextlib.nullcontext() if boom is None else pytest.raises(boom)
    with guard, rec:
        if boom is not None:
            raise boom("target blew up")

    second = Recorder(MemorySink())
    second.start()
    second.stop()


def test_double_start_is_an_error_not_a_noop() -> None:
    """Two things believing they are recording is worse than a loud failure."""
    rec = Recorder(MemorySink())
    rec.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            rec.start()
    finally:
        rec.stop()


def test_broken_sink_never_reaches_the_target() -> None:
    """The invariant that outranks everything.

    Measured on 3.14: a callback raising once injects an exception into the target
    at a line it never wrote, which the target may be unable to catch; a callback
    raising every time takes CPython down its fatal _PyObject_Dump path -- exit 1,
    no traceback. The user's program correctness outranks our recording.
    """

    class BrokenSink:
        def emit(self, event: Event) -> None:
            raise OSError("no space left on device")

        def close(self) -> None:
            pass

    def target() -> int:
        total = 0
        for i in range(5):
            total += i
        return total

    rec = Recorder(BrokenSink())
    with rec:
        result = target()

    assert result == 10, "the target must complete correctly despite a dead sink"
    assert rec.dropped > 0, "drops must be counted, so the recording is known incomplete"


def test_scope_can_be_injected() -> None:
    """Scope is user-configurable; an empty root set records nothing."""
    sink = MemorySink()
    everything_excluded = Scope(roots=[])
    with Recorder(sink, scope=everything_excluded):
        sum(range(5))
    assert sink.events == [], "an all-excluding scope must record nothing"


# un-skipped on day 6: the frame registry makes this pass
def test_generator_frames_suspend_and_resume() -> None:
    """A generator's frame leaves without returning and re-enters later.

    PY_START fires once; the frame then suspends at YIELD and resumes without a
    matching PY_RETURN, so today's stack mis-nests everything after it. Day 6
    replaces the stack with a live-frame registry and un-skips this.
    """

    def gen() -> Any:
        yield 1
        yield 2

    def run() -> int:
        return sum(gen())

    sink, _ = _record(run)
    depth = 0
    for e in sink.events:
        if e.kind is EventKind.CALL:
            depth += 1
        elif e.kind is EventKind.RETURN:
            depth -= 1
    assert depth == 0, "generator frames leave the stack unbalanced"


def test_values_are_captured_when_the_flag_is_on() -> None:
    """The flag exists so days 40-41 can profile control flow and capture apart.

    Day 3 measured value capture as the dominant cost (2,370x naive). Bolting it
    in unconditionally would mean that when the combined figure disappoints, there
    is no way to tell which half is at fault.
    """

    def run() -> int:
        total = 7
        return total

    sink, _ = _record(run, capture_values=True)
    writes = [e for e in sink.events if e.kind is EventKind.VAR_WRITE]
    assert writes, "capture_values=True must emit VAR_WRITE events"
    assert all(e.value_ref is not None for e in writes)
    assert all(e.name_id is not None for e in writes)


def test_control_flow_is_identical_with_and_without_capture() -> None:
    """Capture must add events, never change which lines ran.

    If turning capture on altered control flow, the recorder would be changing the
    program it observes -- the failure the whole no-user-code rule exists to
    prevent, showing up one layer higher.
    """

    def run() -> int:
        total = 0
        for i in range(3):
            total += i
        return total

    def control_flow(sink: MemorySink, rec: Recorder) -> list[tuple[str, int]]:
        return [
            (e.kind.name, e.lineno)
            for e in sink.events
            if e.kind is not EventKind.VAR_WRITE and rec._codes.resolve(e.code_id) is run.__code__
        ]

    without = control_flow(*_record(run, capture_values=False))
    with_values = control_flow(*_record(run, capture_values=True))
    assert without == with_values
