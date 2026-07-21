"""Stepping, checked three ways: by hand, by symmetry, and against the day-20 oracle.

The handwritten expectations on `examples/simple.py` are the centre of gravity. A human
read that 53-event stream and wrote down where each command should land; if the code and
the human disagree, the *code* is what gets questioned. Nothing else here can replace
that -- a property test proves stepping is self-consistent, not that it is what a
developer means by "step over".
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from chronotrace.reconstruct import (
    Direction,
    Edge,
    KeyframeReconstructor,
    reconstruct_slow,
    seek,
    step,
    step_out,
    step_over,
)
from chronotrace.recorder import Event, EventKind, MemorySink, Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, ChronoWriter

EXAMPLES = Path(__file__).parent.parent.parent / "examples"
BACK = Direction.BACKWARD
ORACLE_SAMPLES = 150
"""Backward steps checked against the O(seq) oracle per example. The examples are 52-149
events, so this covers every one of them -- the cap exists so a longer example added later
degrades the runtime instead of the suite."""

# The LINE instants of examples/simple.py, recorded from `simple.main`. Every expectation
# below was read off that stream by hand. If this list changes, a new interpreter emits a
# different stream -- regenerate it and re-derive the expectations, rather than nudging
# them one at a time until they pass.
LINE_SEQS = [1, 2, 4, 7, 10, 12, 15, 18, 20, 23, 26, 27, 30, 33, 35, 38, 41, 43, 46, 49, 51]


def _record(module: str) -> tuple[list[Event], Recorder]:
    """Record one example program, scoped to `examples/` so the stream is only its own."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        imported: Any = __import__(module)
        sink = MemorySink()
        recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(EXAMPLES)]))
        with recorder:
            imported.main()
    finally:
        sys.path.remove(str(EXAMPLES))
    return sink.events, recorder


def _write(
    events: list[Event], recorder: Recorder | None = None, *, truncated: bool = False
) -> ChronoReader:
    """A real `.chrono` over those events -- small blocks, so keyframes actually appear."""
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=16, keyframe_interval=8)
    for captured in recorder.values if recorder is not None else ():
        writer.add_value(captured)
    for event in events:
        writer.add(event)
    writer.close(truncated=truncated)
    return ChronoReader.from_bytes(buf.getvalue())


def _events(reader: ChronoReader) -> list[Event]:
    return reader[0 : len(reader)]  # type: ignore[return-value]


def _at(reader: ChronoReader, seq: int) -> Event:
    return reader[seq]  # type: ignore[return-value]


@pytest.fixture(scope="module")
def simple() -> ChronoReader:
    return _write(*_record("simple"))


@pytest.fixture(scope="module")
def generators() -> ChronoReader:
    return _write(*_record("generators"))


@pytest.fixture(scope="module")
def exceptions() -> ChronoReader:
    return _write(*_record("exceptions"))


def test_the_golden_stream_is_what_a_human_verified(simple: ChronoReader) -> None:
    """Guards every handwritten seq below. Read this failure before any other in the file."""
    assert [e.seq for e in _events(simple) if e.kind == EventKind.LINE] == LINE_SEQS


# -- the four operations, against seqs a human read off the stream ----------------------


def test_step_back_enters_the_call_that_just_finished(simple: ChronoReader) -> None:
    """seq 15 is `LINE quadruple:24`, one event after `double` returned at 14.

    The previous line *anywhere* is 12, `result = n * 2` inside the call that just
    completed -- backward "step into". A debugger that landed on 7 instead would have
    skipped a whole function's history.
    """
    assert step(simple, 15, BACK) == 12


def test_step_over_back_skips_a_completed_call(simple: ChronoReader) -> None:
    """From the same instant, staying in `quadruple`'s frame skips seqs 8-14 entirely."""
    assert step_over(simple, 15, BACK) == 7


def test_step_over_back_skips_a_whole_call_tree(simple: ChronoReader) -> None:
    """seq 26 is back in `main` after `quadruple` ran two nested `double`s.

    The previous line in `main`'s frame is 4 -- twenty-one events skipped by one command,
    which is what a state walk would have paid for delta by delta.
    """
    assert step_over(simple, 26, BACK) == 4


def test_step_over_back_uses_frame_id_not_code_identity(simple: ChronoReader) -> None:
    """`double` runs four times, so four frames share one `code_id`.

    From seq 20 (frame 4), the previous line in *that invocation* is 18. A filter on
    `code_id` would return 12 -- the same source line in a different call -- and the
    debugger would silently teleport between invocations.
    """
    assert step_over(simple, 20, BACK) == 18
    assert step_over(simple, 18, BACK) is Edge.BEGINNING  # frame 4 has no earlier line


def test_step_out_back_reaches_the_call_that_entered_the_frame(simple: ChronoReader) -> None:
    assert step_out(simple, 20, BACK) == 17  # the CALL of double's third frame
    assert step_out(simple, 15, BACK) == 6  # the CALL of quadruple


def test_step_out_forward_reaches_the_frame_exit(simple: ChronoReader) -> None:
    assert step_out(simple, 10) == 14  # RETURN of double
    assert step_out(simple, 1) == 52  # RETURN of main, the last event


def test_seek_is_reverse_continue(simple: ChronoReader) -> None:
    """`continue_back`, generalised: the previous instant where a predicate holds."""

    def at_line_18(event: Event) -> bool:
        return event.kind == EventKind.LINE and event.lineno == 18

    assert seek(simple, 44, BACK, at_line_18) == 41
    assert seek(simple, 10, BACK, at_line_18) is Edge.BEGINNING


# -- boundaries: values, never exceptions ----------------------------------------------


def test_stepping_back_from_the_beginning_is_a_value(simple: ChronoReader) -> None:
    """seq 0 is a CALL, so even seq 1 has no previous *line*. Both report the edge."""
    assert step(simple, 0, BACK) is Edge.BEGINNING
    assert step(simple, 1, BACK) is Edge.BEGINNING


def test_stepping_past_the_end_is_a_value(simple: ChronoReader) -> None:
    assert step(simple, len(simple) - 1) is Edge.END


def test_a_lost_tail_is_not_the_end_of_the_program() -> None:
    """A crash-truncated recording must not claim the program ended where the file did."""
    events, recorder = _record("simple")
    reader = _write(events, recorder, truncated=True)
    assert reader.truncated
    assert step(reader, len(reader) - 1) is Edge.LOST_TAIL


def test_step_out_back_from_a_frame_with_no_recorded_call() -> None:
    """A frame already running when recording began was never *entered* on the record.

    `BEGINNING` is the truth. Inventing a call site would be worse than admitting the
    recording does not contain one.
    """
    frame = [
        Event(
            seq=n,
            kind=EventKind.LINE,
            timestamp_ns=n,
            thread_id=1,
            frame_id=9,
            code_id=1,
            lineno=10 + n,
        )
        for n in range(3)
    ]
    reader = _write(frame)
    assert step_out(reader, 2, BACK) is Edge.BEGINNING


def test_an_invalid_instant_raises(simple: ChronoReader) -> None:
    """A boundary is a value; a `seq` that never existed is a caller bug."""
    with pytest.raises(IndexError):
        step(simple, len(simple), BACK)
    with pytest.raises(IndexError):
        step(simple, -1)


# -- the day's referee: forward and backward are one code path -------------------------


@pytest.mark.parametrize("name", ["simple", "generators", "exceptions"])
def test_step_back_undoes_step_forward(name: str, request: pytest.FixtureRequest) -> None:
    """`step_back(step_forward(seq)) == seq` at every stop instant, in every example.

    The property that catches forward and backward drifting apart -- the failure that
    makes a time-travel debugger worse than none.
    """
    reader: ChronoReader = request.getfixturevalue(name)
    stops = [e.seq for e in _events(reader) if e.kind == EventKind.LINE]
    assert len(stops) > 10, "an example with no lines proves nothing"
    for seq in stops:
        forward = step(reader, seq)
        if isinstance(forward, Edge):
            assert seq == stops[-1]  # only the last stop has nowhere to go
            continue
        assert step(reader, forward, BACK) == seq


@pytest.mark.parametrize("name", ["simple", "generators", "exceptions"])
def test_the_round_trip_normalises_a_non_stop_instant(
    name: str, request: pytest.FixtureRequest
) -> None:
    """From *any* instant, the round trip lands on the nearest stop at or before it.

    The total form of the property above: stepping from a `VAR_WRITE` (an instant no
    debugger pauses on) first snaps to the enclosing line, which is why the identity holds
    at stop instants without being special-cased for them.
    """
    reader: ChronoReader = request.getfixturevalue(name)
    stops = [e.seq for e in _events(reader) if e.kind == EventKind.LINE]
    for seq in range(len(reader)):
        forward = step(reader, seq)
        if isinstance(forward, Edge):
            continue
        earlier = [s for s in stops if s <= seq]
        assert step(reader, forward, BACK) == (earlier[-1] if earlier else Edge.BEGINNING)


def test_step_over_back_undoes_step_over(simple: ChronoReader) -> None:
    """The same referee for the frame-filtered pair."""
    for event in _events(simple):
        if event.kind != EventKind.LINE:
            continue
        forward = step_over(simple, event.seq)
        if isinstance(forward, Edge):
            continue
        assert step_over(simple, forward, BACK) == event.seq


# -- generators and recursion: documented behaviour, not accidental --------------------


def test_step_back_across_a_yield_lands_in_the_consumer(generators: ChronoReader) -> None:
    """The documented surprise: execution order, not the call stack.

    At the instant a generator resumes, the previous line that ran belongs to whoever
    called `next()`. A stack model says the consumer is "above" and should not be where
    backward stepping goes; execution says otherwise, and execution is what was recorded.
    """
    resumes = [e for e in _events(generators) if e.kind == EventKind.RESUME]
    assert resumes, "examples/generators.py must exercise suspension"
    crossed = sum(
        1
        for r in resumes
        if isinstance(back := step(generators, r.seq, BACK), int)
        and _at(generators, back).frame_id != r.frame_id
    )
    assert crossed, "no backward step crossed a suspension boundary"


@pytest.mark.parametrize("name", ["generators", "exceptions"])
def test_step_over_back_never_leaves_its_frame(name: str, request: pytest.FixtureRequest) -> None:
    """The operation that *does* respect the frame -- the advice the docstring gives users.

    Holds through suspension (a generator's own previous line, not its consumer's) and
    through `asyncio` interleaving (other tasks' events are simply not in this frame).
    """
    reader: ChronoReader = request.getfixturevalue(name)
    for event in _events(reader):
        if event.kind != EventKind.LINE:
            continue
        back = step_over(reader, event.seq, BACK)
        if isinstance(back, Edge):
            continue
        assert _at(reader, back).frame_id == event.frame_id


def test_step_over_back_stays_in_one_recursive_invocation() -> None:
    """Recursion is the case `code_id` gets wrong and `frame_id` gets right.

    Recorded from a local function rather than an example file so the recursion depth and
    the expectation sit next to each other.
    """
    reader = _record_here(lambda: _countdown(6))
    deepest = max(e.frame_id for e in _events(reader))
    assert deepest >= 6, "the fixture must actually recurse"
    for event in _events(reader):
        if event.kind != EventKind.LINE:
            continue
        back = step_over(reader, event.seq, BACK)
        if isinstance(back, Edge):
            continue
        assert _at(reader, back).frame_id == event.frame_id


def test_stepping_around_a_frame_that_died_by_exception(exceptions: ChronoReader) -> None:
    """A frame that unwound is still fully walkable -- it just ended differently.

    `step_out` forward must find the `UNWIND`, not run past it looking for a `RETURN` that
    never came, and `step_over_back` from it must land on the frame's own last line -- not
    on the deeper frame that actually raised, which is what plain `step_back` finds and is
    exactly the distinction between the two commands. Nothing in the search cares how a
    frame ended, which is the point.
    """
    unwinds = [e for e in _events(exceptions) if e.kind == EventKind.UNWIND]
    assert unwinds, "examples/exceptions.py must exercise propagation"
    for unwind in unwinds:
        inside = [
            e.seq
            for e in _events(exceptions)
            if e.frame_id == unwind.frame_id and e.kind == EventKind.LINE and e.seq < unwind.seq
        ]
        if not inside:
            continue
        assert step_out(exceptions, inside[0]) == unwind.seq
        assert step_over(exceptions, unwind.seq, BACK) == inside[-1]


def _countdown(n: int) -> int:
    if n <= 0:
        return 0
    return 1 + _countdown(n - 1)


def _record_here(fn: Callable[[], object]) -> ChronoReader:
    """Record `fn`, scoped to this test file, so the stream is only what the test wrote."""
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(Path(__file__).parent)]))
    with recorder:
        fn()
    return _write(sink.events, recorder)


# -- differential: the state at every backward destination equals the oracle's ---------


@pytest.mark.parametrize("name", ["simple", "generators", "exceptions"])
def test_every_backward_step_lands_on_the_oracle_state(
    name: str, request: pytest.FixtureRequest
) -> None:
    """Stepping chooses a `seq`; reconstruction must agree with the slow truth there.

    Driven through one `KeyframeReconstructor` so a real backward drag exercises the
    locality cache -- a cache that drifts backward is exactly what this catches.
    """
    reader: ChronoReader = request.getfixturevalue(name)
    fast = KeyframeReconstructor(reader)
    seq: int | Edge = len(reader) - 1
    visited = 0
    while not isinstance(seq, Edge) and visited < ORACLE_SAMPLES:
        assert fast.reconstruct(seq) == reconstruct_slow(reader, seq), f"drift at seq {seq}"
        visited += 1
        seq = step(reader, seq, BACK)
    assert visited > 10


def test_a_long_backward_drag_stays_correct_and_monotonic(reader: ChronoReader) -> None:
    """Thousands of backward steps over the synthetic recording, starting from the end.

    Correctness only: the latency budget belongs to `benchmarks/bench_stepping.py`, where
    a slow CI machine reports a number instead of failing a build.
    """
    fast = KeyframeReconstructor(reader)
    seq: int | Edge = len(reader) - 1
    steps = 0
    while not isinstance(seq, Edge) and steps < 10_000:
        previous = seq
        seq = step(reader, seq, BACK)
        if isinstance(seq, Edge):
            break
        assert seq < previous, "a backward step must move backward"
        assert fast.reconstruct(seq).seq == seq
        steps += 1
    assert steps > 1000
