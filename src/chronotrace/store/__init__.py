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
Being filled in during Phase 2 (days 11-18). Day 11 designed the format: the
normative spec is [`docs/format-spec.md`](../../../docs/format-spec.md) and its
machine form -- every magic byte, struct and block tag -- is `constants.py`, the
single source of truth that a reader in any language must agree with. Day 12
writes the encoder/decoder against it; nothing here reads or writes bytes yet.

What this package must NEVER import
-----------------------------------
``chronotrace.index``, ``.reconstruct``, ``.query``, ``.server``.

It may import ``chronotrace.recorder`` for the event model only -- the store has
to know what an event *is* in order to serialise one. It must never reach back
into the recorder's runtime machinery (monitoring callbacks, frame registry);
that direction of knowledge would couple the file format to the mechanics of
observation, and the two must be free to change independently.
"""
