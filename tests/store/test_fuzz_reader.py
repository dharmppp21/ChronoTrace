"""Fuzz the reader: random bytes must never hang, crash, or OOM `ChronoReader`.

Opening a `.chrono` is the normal workflow for a file from a stranger's bug report, so
the *only* acceptable failure on malformed input is a precise `ChronoError`. Anything
else -- an unhandled exception, an unbounded allocation, a hang -- is a vulnerability.
Every code path the reader walks is bounded (recovery.walk_blocks is O(file size), every
decode caps its allocations), so this asserts that property holds over thousands of
random and semi-structured inputs."""

from __future__ import annotations

import contextlib
import random
import tracemalloc

from hypothesis import given, settings
from hypothesis import strategies as st

from chronotrace.store import ChronoError, ChronoReader
from chronotrace.store.constants import FORMAT_VERSION_MINOR, HEADER, HEADER_SIZE, MAGIC

_ITERATIONS = 5000  # random inputs; the loop is fast because most fail the magic check early
_VALID_HEADER = HEADER.pack(MAGIC, 1, FORMAT_VERSION_MINOR, 0, HEADER_SIZE)


def _drive(data: bytes) -> None:
    """Open and fully read `data`. A `ChronoError` is fine; nothing else may escape."""
    try:
        reader = ChronoReader.from_bytes(data)
        list(reader.iter_events())  # force every block to decode
        reader.nearest_keyframe_at_or_before(len(reader))  # force the keyframe/pool paths
        for ref in range(min(len(reader), 3)):
            with contextlib.suppress(IndexError, ChronoError):
                reader.value(ref)
    except ChronoError:
        pass  # the one acceptable outcome on malformed input


def _random_input(rng: random.Random) -> bytes:
    """A random input, half pure noise and half a valid header over noise -- the second
    kind is what actually exercises the scan-recovery and block-decode paths."""
    body = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 300)))
    return _VALID_HEADER + body if rng.random() < 0.5 else body


def test_random_bytes_never_crash_or_oom_the_reader() -> None:
    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    tracemalloc.start()
    for _ in range(_ITERATIONS):
        _drive(_random_input(rng))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    # No single small input, nor the whole run, should have provoked a large allocation.
    assert peak < 50_000_000, f"fuzzing allocated {peak} bytes -- an input was trusted"


@settings(max_examples=1500, deadline=None)
@given(st.binary(max_size=2000))
def test_hypothesis_binary_is_only_ever_a_clean_error(data: bytes) -> None:
    _drive(data)


@settings(max_examples=1000, deadline=None)
@given(st.binary(max_size=2000))
def test_hypothesis_binary_after_a_valid_header(data: bytes) -> None:
    _drive(_VALID_HEADER + data)  # force the recovery path past the header check
