"""Persists the event stream to the ``.chrono`` file format, and reads it back.

This package owns the on-disk contract: block framing and checksums, columnar
encoding, compression, the content-addressed value pool, keyframes, invertible
deltas, memory-mapped reads and crash recovery.

Two properties shape everything here. The format is **append-only**, which is
what later makes recordings immutable and therefore trivially cacheable. And a
``.chrono`` file is **untrusted input** -- recordings get shared in bug reports,
so the reader must treat every length and offset in the file as hostile until
validated.

Public surface
--------------
`FileSink` (a `Sink` that records to a `.chrono` file) and `ChronoReader` (a
read-only, mmap-backed, lazily-decoded view over one) are the API every layer
above uses; `ChronoWriter` is the lower-level stream writer beneath `FileSink`,
and the `ChronoError` hierarchy is what a caller catches. The normative byte
layout is [`docs/format-spec.md`](../../../docs/format-spec.md); its machine form
is `constants.py`.

What this package must NEVER import
-----------------------------------
``chronotrace.index``, ``.reconstruct``, ``.query``, ``.server``.

It may import ``chronotrace.recorder`` for the event model only -- the store has
to know what an event *is* in order to serialise one. It must never reach back
into the recorder's runtime machinery (monitoring callbacks, frame registry);
that direction of knowledge would couple the file format to the mechanics of
observation, and the two must be free to change independently.
"""

from chronotrace.store.delta import Delta, apply, invert, state_from_keyframe
from chronotrace.store.errors import (
    ChronoError,
    CorruptRecording,
    TruncatedRecording,
    UnsupportedVersion,
)
from chronotrace.store.keyframe import FrameSnapshot, Keyframe
from chronotrace.store.reader import ChronoReader
from chronotrace.store.recovery import repair
from chronotrace.store.strings import CodeInfo, Strings
from chronotrace.store.writer import ChronoWriter, FileSink

__all__ = [
    "ChronoError",
    "ChronoReader",
    "ChronoWriter",
    "CodeInfo",
    "CorruptRecording",
    "Delta",
    "FileSink",
    "FrameSnapshot",
    "Keyframe",
    "Strings",
    "TruncatedRecording",
    "UnsupportedVersion",
    "apply",
    "invert",
    "repair",
    "state_from_keyframe",
]
