"""Four invariants, each stated as an equation and checked over generated input.

The pipeline property in `test_pipeline.py` is the one that matters, but it answers only
"is the whole thing right?". When it goes red these say *which layer*, which is the
difference between a bisect and a guess. Each is an equation the design already claimed:

    invert(apply(s, d)) == s                 day 16 -- deltas are invertible
    read(write(events)) == events            days 12-13 -- the codec round-trips
    step_back(step_forward(seq)) == seq      day 21 -- stepping is symmetric
    replay depth <= keyframe interval        day 15 -- reconstruction cost is bounded

The first two are checked over generated *values* (states, deltas, event streams) rather
than generated programs, because they are algebraic and a program is an expensive way to
produce a delta. The last two need real recordings, so they ride on the program generator.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chronotrace.reconstruct import Direction, Edge, step
from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, ChronoWriter
from chronotrace.store.delta import Delta, DeltaKind, apply, invert
from tests.equivalence import record

from . import load_generated
from .program_gen import python_program

pytestmark = pytest.mark.filterwarnings("ignore::SyntaxWarning")

_REFS = st.integers(-1, 20)
_IDS = st.integers(1, 6)


@st.composite
def _state_and_delta(draw: st.DrawFn) -> tuple[dict[int, dict[int, int]], Delta]:
    """A live-frame state and a delta that legally applies to it.

    The delta is drawn *from* the state rather than independently, because `apply` is
    documented to raise on a delta that does not fit -- and testing that a bad delta
    raises is a different property from testing that a good one inverts.
    """
    state = draw(
        st.dictionaries(
            _IDS, st.dictionaries(_IDS, st.integers(0, 20), max_size=4), min_size=1, max_size=3
        )
    )
    frame = draw(st.sampled_from(sorted(state)))
    kind = draw(st.sampled_from([DeltaKind.BIND, DeltaKind.FRAME_EXIT]))
    if kind is DeltaKind.BIND:
        name = draw(_IDS)
        return state, Delta(kind, 0, frame, name, state[frame].get(name, -1), draw(_REFS))
    return state, Delta(kind, 0, frame, frame_locals=tuple(state[frame].items()))


@given(_state_and_delta())
@settings(max_examples=200)
def test_a_delta_is_invertible(pair: tuple[dict[int, dict[int, int]], Delta]) -> None:
    """Day 16's core claim, and the reason `old_ref` is stored at all."""
    state, delta = pair
    assert invert(apply(state, delta), delta) == state


@st.composite
def _event_stream(draw: st.DrawFn) -> list[Event]:
    """A well-formed event stream: dense seqs, plausible frames, some values."""
    count = draw(st.integers(1, 60))
    kinds = st.sampled_from([EventKind.LINE, EventKind.CALL, EventKind.RETURN])
    return [
        Event(
            seq=i,
            kind=draw(kinds),
            timestamp_ns=1000 + i,
            thread_id=1,
            frame_id=draw(_IDS),
            code_id=draw(st.integers(0, 4)),
            lineno=draw(st.integers(0, 200)),
            name_id=None,
            value_ref=None,
            exc_type_id=None,
        )
        for i in range(count)
    ]


@given(_event_stream(), st.integers(2, 32))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_the_codec_round_trips(events: list[Event], block: int) -> None:
    """Days 12-13: what the writer wrote is exactly what the reader reads back.

    `block_events` is drawn so streams straddle block boundaries -- the seam where a
    columnar codec gets its off-by-ones.
    """
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=block, keyframe_interval=8)
    for event in events:
        writer.add(event)
    writer.close()
    reader = ChronoReader.from_bytes(buf.getvalue())
    assert len(reader) == len(events)
    for original, restored in zip(events, reader.iter_events(), strict=True):
        assert (original.seq, original.kind, original.frame_id, original.lineno) == (
            restored.seq,
            restored.kind,
            restored.frame_id,
            restored.lineno,
        )


@given(python_program(), st.integers(1, 16))
@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
def test_stepping_is_symmetric_on_generated_programs(
    layers_dir: Path, source: str, interval: int
) -> None:
    """Day 21's referee, on programs nobody wrote.

    Forward and backward are one function with the opposite sign, so this can only fail
    if that stops being true -- which is exactly what it is here to notice.
    """
    reader = _recording(layers_dir, source, interval)
    stops = [e.seq for e in reader[0 : len(reader)] if e.kind is EventKind.LINE]  # type: ignore[union-attr]
    for seq in stops:
        forward = step(reader, seq)
        if isinstance(forward, Edge):
            continue
        assert step(reader, forward, Direction.BACKWARD) == seq


@given(python_program(), st.integers(1, 16))
@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
def test_replay_depth_stays_within_the_keyframe_interval(
    layers_dir: Path, source: str, interval: int
) -> None:
    """ADR-0006's cost proof, asserted rather than assumed, at every interval.

    Reaching any instant must replay at most `interval` events from its keyframe. A
    violation means the writer's cadence broke, not that reconstruction is slow -- which
    is why this measures the *distance*, not the clock.
    """
    reader = _recording(layers_dir, source, interval)
    for seq in range(len(reader)):
        keyframe = reader.nearest_keyframe_at_or_before(seq)
        assert keyframe is not None, f"no keyframe at or before seq {seq}"
        assert seq - keyframe.seq <= interval, f"seq {seq} is {seq - keyframe.seq} past a keyframe"


@pytest.fixture(scope="module")
def layers_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("layers")


def _recording(workdir: Path, source: str, interval: int) -> ChronoReader:
    """Record a generated program at a given keyframe cadence."""
    return record(
        load_generated(workdir, source),
        Scope(roots=[str(workdir)]),
        keyframe_interval=interval,
        block_events=16,
    ).reader
