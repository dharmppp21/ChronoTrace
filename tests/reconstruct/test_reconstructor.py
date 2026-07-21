"""The fast path's edges: the floor, keyframe instants, the last event, out-of-range,
a truncated recording, a torn keyframe, and lazy value resolution.

Equality with the oracle is `test_differential.py`; this file pins the behaviours that
have no oracle to compare against -- what happens at the boundaries of the recording
itself.
"""

from __future__ import annotations

import pytest
from conftest import (  # type: ignore[import-not-found]
    KEYFRAME_INTERVAL,
    build_events,
    write_recording,
)

from chronotrace.reconstruct import (
    KeyframeReconstructor,
    MissingValue,
    ValueResolver,
    reconstruct_slow,
)
from chronotrace.reconstruct.types import NO_FRAME
from chronotrace.store import ChronoReader


def test_seq_zero_is_the_floor(reader: ChronoReader) -> None:
    """Keyframe 0 always exists (day 15), so the first instant needs no replay."""
    state = KeyframeReconstructor(reader).reconstruct(0)
    assert state.seq == 0
    assert state.current_frame_id != NO_FRAME  # the first event's frame is executing


def test_a_keyframe_instant_replays_nothing_and_the_last_event_works(
    reader: ChronoReader,
) -> None:
    fast = KeyframeReconstructor(reader, use_cache=False)
    at_keyframe = fast.reconstruct(KEYFRAME_INTERVAL)
    assert at_keyframe.seq == KEYFRAME_INTERVAL
    last = fast.reconstruct(len(reader) - 1)
    assert last.seq == len(reader) - 1


@pytest.mark.parametrize("bad", [-1, 10**9])
def test_out_of_range_raises_and_never_clamps(reader: ChronoReader, bad: int) -> None:
    """Clamping would invent a state the program was never in."""
    with pytest.raises(IndexError):
        KeyframeReconstructor(reader).reconstruct(bad)
    with pytest.raises(IndexError):
        reconstruct_slow(reader, bad)


def test_a_suspended_generator_is_live_in_the_state(reader: ChronoReader) -> None:
    """The synthetic recording yields generators; at some instant one must be live and
    suspended -- live-but-not-on-a-stack is the day-6 model reconstruction inherits."""
    fast = KeyframeReconstructor(reader, use_cache=False)
    assert any(
        any(f.suspended for f in fast.reconstruct(seq).frames) for seq in range(0, len(reader), 7)
    ), "no instant had a suspended generator -- the fixture is not exercising it"


def test_a_frame_that_entered_before_the_keyframe_is_still_live(reader: ChronoReader) -> None:
    """The outermost frame entered at seq 0 and never returns, so every reconstruction
    far from the start must still carry it -- it comes from the keyframe, not the window."""
    state = KeyframeReconstructor(reader).reconstruct(len(reader) - 1)
    assert state.frame(2) is not None  # the `main` frame (first id handed out)


# ---------------------------------------------------------------------------
# Damaged recordings
# ---------------------------------------------------------------------------


def test_a_truncated_recording_reconstructs_its_prefix_and_raises_past_it() -> None:
    full = write_recording(build_events(3000))
    reader = ChronoReader.from_bytes(full[: len(full) * 2 // 3])
    assert reader.truncated is True
    n = len(reader)
    assert KeyframeReconstructor(reader).reconstruct(n - 1).seq == n - 1  # prefix works
    with pytest.raises(IndexError):
        KeyframeReconstructor(reader).reconstruct(n)  # in the lost tail: no state exists


def test_a_torn_keyframe_falls_back_and_still_matches_the_oracle() -> None:
    """A keyframe whose CRC fails is skipped for the previous one (day 15) and the
    reconstruction simply replays further -- the answer must be identical."""
    data = bytearray(write_recording(build_events(3000)))
    probe = ChronoReader.from_bytes(bytes(data))
    _seq, offset = probe._keyframes[3]  # a mid-recording keyframe
    data[offset + 12 + 8 + 2] ^= 0xFF  # corrupt its compressed payload -> CRC fails

    reader = ChronoReader.from_bytes(bytes(data))
    fast = KeyframeReconstructor(reader, use_cache=False)
    target = probe._keyframes[3][0] + 5  # just past the torn keyframe
    assert fast.reconstruct(target) == reconstruct_slow(reader, target)


# ---------------------------------------------------------------------------
# Lazy value resolution
# ---------------------------------------------------------------------------


def test_values_resolve_lazily_through_the_pool(reader: ChronoReader) -> None:
    state = KeyframeReconstructor(reader).reconstruct(len(reader) // 2)
    resolver = ValueResolver(reader)
    bindings = next(f.bindings for f in state.frames if f.bindings)
    name_id, ref = next(iter(bindings.items()))
    value = resolver.resolve(ref)
    assert value == resolver.resolve(ref)  # second call is an LRU hit, same object value
    assert resolver.resolve_bindings(bindings)[name_id] == value


def test_a_missing_value_ref_raises_rather_than_pretending_to_be_none(
    reader: ChronoReader,
) -> None:
    """Returning None would render as 'the variable was None' -- a lie about the program."""
    with pytest.raises(MissingValue):
        ValueResolver(reader).resolve(10**7)


def test_the_resolver_lru_is_bounded(reader: ChronoReader) -> None:
    resolver = ValueResolver(reader, max_entries=4)
    for ref in range(10):
        resolver.resolve(ref)
    assert len(resolver._cache) == 4
