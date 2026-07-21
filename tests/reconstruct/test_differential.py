"""The headline test: the fast path equals the obviously-correct oracle, everywhere.

`reconstruct_slow` replays from `seq` 0 and cannot plausibly be wrong -- no keyframe to
decode, no window to get off by one, no cache to drift. So any disagreement means the
*fast* path is wrong. This is differential testing, and it is the pattern that makes a
subtle algorithm safe to ship: the day-21 backward step and the day-22 harness both lean
on the same oracle.

Both paths share their *apply* rules (`_replay`) and differ only in the range they
replay, which is exactly the surface where the bugs live: which keyframe, which delta
range, and the boundaries between them.
"""

from __future__ import annotations

import random

from chronotrace.reconstruct import KeyframeReconstructor, reconstruct_slow
from chronotrace.store import ChronoReader

from .conftest import BLOCK_EVENTS, KEYFRAME_INTERVAL


def _boundary_seqs(n: int) -> list[int]:
    """Every place an off-by-one hides: the floor, keyframe instants and their
    neighbours, block edges, and the last event."""
    seqs = {0, 1, 2, n - 1, n - 2}
    for k in range(0, n, KEYFRAME_INTERVAL):  # keyframe instants and +-1
        seqs.update({k - 1, k, k + 1})
    for b in range(0, n, BLOCK_EVENTS):  # block edges: a different decode path
        seqs.update({b - 1, b, b + 1})
    return sorted(s for s in seqs if 0 <= s < n)


def test_fast_equals_oracle_at_every_boundary(reader: ChronoReader) -> None:
    fast = KeyframeReconstructor(reader, use_cache=False)
    for seq in _boundary_seqs(len(reader)):
        assert fast.reconstruct(seq) == reconstruct_slow(reader, seq), f"disagree at seq {seq}"


def test_fast_equals_oracle_at_random_seqs(reader: ChronoReader) -> None:
    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    fast = KeyframeReconstructor(reader, use_cache=False)
    for seq in (rng.randrange(len(reader)) for _ in range(120)):
        assert fast.reconstruct(seq) == reconstruct_slow(reader, seq), f"disagree at seq {seq}"


def test_cached_equals_uncached_under_every_access_pattern(reader: ChronoReader) -> None:
    """A drifting cache is a silent lie -- the one failure a debugger cannot survive. The
    cached reconstructor must be indistinguishable from the uncached one under a forward
    drag, a backward drag, random jumps, and repeats."""
    rng = random.Random(1)  # noqa: S311 -- reproducibility, not security
    n = len(reader)
    cached = KeyframeReconstructor(reader, use_cache=True)
    uncached = KeyframeReconstructor(reader, use_cache=False)

    patterns: list[int] = []
    patterns += list(range(400, 460))  # forward drag
    patterns += list(range(460, 400, -1))  # backward drag
    patterns += [rng.randrange(n) for _ in range(40)]  # random jumps
    patterns += [900, 900, 901, 901]  # repeats
    patterns += list(range(KEYFRAME_INTERVAL - 3, KEYFRAME_INTERVAL + 4))  # across a keyframe

    for seq in patterns:
        assert cached.reconstruct(seq) == uncached.reconstruct(seq), f"cache drifted at seq {seq}"


def test_cached_matches_the_oracle_too(reader: ChronoReader) -> None:
    """Belt and braces: the cached path is checked against the truth, not only against the
    uncached path (which shares its code)."""
    cached = KeyframeReconstructor(reader, use_cache=True)
    for seq in sorted(_boundary_seqs(len(reader)))[:40]:
        assert cached.reconstruct(seq) == reconstruct_slow(reader, seq), f"disagree at seq {seq}"
