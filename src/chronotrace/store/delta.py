"""Deltas: exactly what changed between two instants, invertibly.

Keyframes (day 15) give reconstruction a floor; deltas carry it from that floor to any
`seq`. Together they are the codec: state at `seq` S = the nearest keyframe, plus the
deltas from there to S. This file is the delta and the two pure functions over program
state that make it a codec: `apply` (forward) and `invert` (backward).

The invertibility contract, and why it is the phase's core decision
-------------------------------------------------------------------
A binding delta stores **both** the old ref and the new ref, so it can be undone
without any other information: `invert(apply(state, d)) == state`. That is the single
highest-leverage decision in the phase. A forward-only delta (new ref alone) can be
*applied* but not *reversed*, so every backward step -- the thing a time-travel
debugger does constantly -- would have to rewind to the previous keyframe and replay
forward to the target minus one. Storing the old ref (measured cost in
benchmarks/RESULTS.md) buys O(1) backward steps instead of O(interval). Day 21 steps
backward by `invert`; day 22's replay-equivalence harness checks against `apply`.

The state a delta mutates
-------------------------
`State = {frame_id: {name_id: value_ref}}` -- each live frame and its current bindings.
A frame's mere presence means it is live (day-6 registry model: a frame is live from
CALL until RETURN/UNWIND, and a suspended generator is *still live*, so it stays in the
state). This is the binding projection of a keyframe: `state_from_keyframe` builds it
from `Keyframe.frames`, so a keyframe *is* the state a delta mutates -- not a copy of it.

`apply`/`invert` are **pure** (they return a new state, never mutate their input),
because pure is trivially testable: the property `invert(apply(s, d)) == s` over random
states and deltas is this file's referee. Reconstruction may later add an in-place
variant if the copy-per-delta cost is measured to matter; the bound (at most `interval`
deltas from a keyframe) makes that unlikely to be the bottleneck first.

Deletion, and the recorder's blind spot
----------------------------------------
A binding whose new ref is `NO_REF` is a **deletion** (`del x`): `apply` removes it,
and `invert` of a *creation* (old = `NO_REF`) is exactly a deletion -- so deletions are
required for invertibility even though the recorder never emits one. The recorder scans
`frame.f_locals`, which a deleted name simply leaves, so `del x` is currently invisible
to recording (a tracked recorder limitation, not something this layer papers over); the
delta model still handles deletion because inversion produces it.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Final

from chronotrace.recorder.events import Event, EventKind
from chronotrace.store.columnar import pack_columns, unpack_columns
from chronotrace.store.keyframe import FrameSnapshot, Keyframe

NO_REF: Final = -1
"""Sentinel for "no binding": a created binding's `old_ref`, a deleted binding's
`new_ref`. A real `ValueRef` is a non-negative pool index, so -1 can never collide."""

MAX_DELTAS_PER_BLOCK: Final = 1 << 21
"""Untrusted-input bound on a DELTAS block's delta count. One event yields at most two
deltas (an implicit frame-enter plus a bind), so this sits above 2x the event cap."""

MAX_LOCAL_PAIRS_PER_BLOCK: Final = 1 << 24
"""Untrusted-input bound on the total (name, ref) pairs a block's frame-exit deltas
carry, so a hostile block cannot claim a colossal locals region."""

State = dict[int, dict[int, int]]
"""`{frame_id: {name_id: value_ref}}` -- live frames and their current bindings."""

DELTA_RANGE = struct.Struct("<Q Q")
"""The `(first_seq, last_seq)` prefix on a DELTAS payload, uncompressed so the reader
peeks a block's seq span without decompressing it (to answer `deltas_between`)."""
DELTA_RANGE_SIZE = DELTA_RANGE.size

_COUNT = struct.Struct("<I")


class DeltaError(ValueError):
    """A delta applied to a state it does not fit: a missing or duplicate frame.

    Never silently reconciled -- an out-of-order application is a bug to surface, not a
    state to invent.
    """


class DeltaKind(IntEnum):
    """What a delta changes. Written to disk, so it *is* an int."""

    BIND = 1
    """A local binding changed: `(frame_id, name_id, old_ref, new_ref)`."""
    FRAME_ENTER = 2
    """A frame became live. Carries its bindings (empty for a normal call; non-empty
    only as the inverse of a frame-exit)."""
    FRAME_EXIT = 3
    """A frame left. Carries the bindings it had, so it can be inverted back into being."""


@dataclass(frozen=True, slots=True)
class Delta:
    """One invertible change to program state.

    Self-describing: it carries everything `apply` and `invert` need, so it never has to
    consult the event stream.
    """

    kind: DeltaKind
    seq: int
    frame_id: int
    name_id: int = -1
    old_ref: int = NO_REF
    new_ref: int = NO_REF
    frame_locals: tuple[tuple[int, int], ...] = ()  # FRAME_ENTER/EXIT: (name_id, value_ref)


def apply(state: State, delta: Delta) -> State:
    """Return the state after `delta`. Pure: `state` is not mutated.

    Raises:
        DeltaError: the delta references a frame that is missing (BIND/EXIT) or already
            present (ENTER) -- an out-of-order application, surfaced rather than hidden.
    """
    fid = delta.frame_id
    if delta.kind == DeltaKind.BIND:
        frame = state.get(fid)
        if frame is None:
            raise DeltaError(f"BIND to frame {fid}, which is not live")
        bindings = dict(frame)
        if delta.new_ref == NO_REF:
            bindings.pop(delta.name_id, None)  # a deletion
        else:
            bindings[delta.name_id] = delta.new_ref
        return {**state, fid: bindings}
    if delta.kind == DeltaKind.FRAME_ENTER:
        if fid in state:
            raise DeltaError(f"FRAME_ENTER for frame {fid}, which is already live")
        return {**state, fid: dict(delta.frame_locals)}
    if fid not in state:  # FRAME_EXIT
        raise DeltaError(f"FRAME_EXIT for frame {fid}, which is not live")
    return {k: v for k, v in state.items() if k != fid}


def inverse(delta: Delta) -> Delta:
    """The reverse of `delta`: applying it undoes what applying `delta` did."""
    if delta.kind == DeltaKind.BIND:
        return replace(delta, old_ref=delta.new_ref, new_ref=delta.old_ref)
    flipped = DeltaKind.FRAME_EXIT if delta.kind == DeltaKind.FRAME_ENTER else DeltaKind.FRAME_ENTER
    return replace(delta, kind=flipped)


def invert(state: State, delta: Delta) -> State:
    """Step backward: the state *before* `delta` was applied. `invert(apply(s, d)) == s`."""
    return apply(state, inverse(delta))


def state_from_keyframe(keyframe: Keyframe) -> State:
    """The binding state a delta mutates, projected from a keyframe's frames.

    A keyframe *is* this state -- this reads its `local_refs`, it does not define a
    second representation.
    """
    return {f.frame_id: dict(f.local_refs) for f in keyframe.frames}


def derive(event: Event, frames: Mapping[int, FrameSnapshot]) -> list[Delta]:
    """The delta(s) an event produces, given the live frames *before* it is applied.

    `frames` is the writer's `LiveState` (day 15), read for the old ref of a bind and
    for frame liveness -- so the store derives deltas from the state it already tracks,
    never a second one. Returns 0-2 deltas: most events change no binding; a bind for a
    frame first seen mid-recording is preceded by an implicit frame-enter.
    """
    kind, fid, seq = event.kind, event.frame_id, event.seq
    if kind == EventKind.CALL:
        return [Delta(DeltaKind.FRAME_ENTER, seq, fid)]
    if kind in (EventKind.RETURN, EventKind.UNWIND):
        frame = frames.get(fid)
        if frame is None:
            return []  # a frame we never saw live: nothing to retire
        return [Delta(DeltaKind.FRAME_EXIT, seq, fid, frame_locals=tuple(frame.local_refs.items()))]
    if kind == EventKind.VAR_WRITE and event.name_id is not None and event.value_ref is not None:
        frame = frames.get(fid)
        old = NO_REF if frame is None else frame.local_refs.get(event.name_id, NO_REF)
        bind = Delta(DeltaKind.BIND, seq, fid, event.name_id, old, event.value_ref)
        if frame is None:
            return [Delta(DeltaKind.FRAME_ENTER, seq, fid), bind]  # recording began mid-frame
        return [bind]
    return []


def encode_deltas(deltas: list[Delta]) -> bytes:
    """Encode a batch of deltas as a DELTAS block payload -- columnar, like events.

    Seven scalar columns (kind, seq, frame_id, name_id, old_ref, new_ref, local_count)
    plus a two-column region of the frame-exit locals, so repetition (a column of one
    kind, runs of `NO_REF`) is exploited by the codecs and then by zstd. Every write is
    kept -- reconstruction can land on any `seq`; only the bytes are compressed, never
    the events discarded.
    """
    cols = [
        [int(d.kind) for d in deltas],
        [d.seq for d in deltas],
        [d.frame_id for d in deltas],
        [d.name_id for d in deltas],
        [d.old_ref for d in deltas],
        [d.new_ref for d in deltas],
        [len(d.frame_locals) for d in deltas],
    ]
    pair_names = [n for d in deltas for (n, _r) in d.frame_locals]
    pair_refs = [r for d in deltas for (_n, r) in d.frame_locals]
    return (
        _COUNT.pack(len(deltas))
        + pack_columns(cols)
        + _COUNT.pack(len(pair_names))
        + pack_columns([pair_names, pair_refs])
    )


def decode_deltas(payload: bytes) -> list[Delta]:
    """Inverse of `encode_deltas`; parses untrusted input.

    The delta count and the locals-pair count are capped, and the per-delta local counts
    must sum to the region size, so a hostile block cannot over-allocate or read past its
    own bytes.

    Raises:
        ValueError: a count over its cap, an inconsistent locals region, or a bad kind.
        struct.error: the payload is too short for a header it declares.
    """
    (count,) = _COUNT.unpack_from(payload, 0)
    if not 0 <= count <= MAX_DELTAS_PER_BLOCK:
        raise ValueError(f"block claims {count} deltas, over the {MAX_DELTAS_PER_BLOCK} cap")
    cols, pos = unpack_columns(payload, _COUNT.size, 7, count)
    kinds, seqs, fids, names, olds, news, lcounts = cols
    (npairs,) = _COUNT.unpack_from(payload, pos)
    if not 0 <= npairs <= MAX_LOCAL_PAIRS_PER_BLOCK:
        raise ValueError(f"block claims {npairs} local pairs, over the cap")
    if any(lc < 0 for lc in lcounts) or sum(lcounts) != npairs:
        raise ValueError("delta local counts do not match the locals region")
    (pair_names, pair_refs), _end = unpack_columns(payload, pos + _COUNT.size, 2, npairs)
    return _rebuild(kinds, seqs, fids, names, olds, news, lcounts, pair_names, pair_refs)


def _rebuild(
    kinds: list[int],
    seqs: list[int],
    fids: list[int],
    names: list[int],
    olds: list[int],
    news: list[int],
    lcounts: list[int],
    pair_names: list[int],
    pair_refs: list[int],
) -> list[Delta]:
    deltas: list[Delta] = []
    p = 0
    for i in range(len(kinds)):
        lc = lcounts[i]
        locals_ = tuple(zip(pair_names[p : p + lc], pair_refs[p : p + lc], strict=True))
        p += lc
        deltas.append(
            Delta(DeltaKind(kinds[i]), seqs[i], fids[i], names[i], olds[i], news[i], locals_)
        )
    return deltas
