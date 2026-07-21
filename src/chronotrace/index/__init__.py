"""Makes the past **queryable**: "every write to `total`", "where did this exception start?".

Reconstruction (phase 3) answers *"what was the state at instant S?"*. That is one
question at a time, and it is the wrong shape for the questions people actually ask while
debugging, which are about the whole timeline at once: *when* did this change, *who* wrote
it, *where* did it come from. Answering those by replaying the recording is O(events) per
question. This layer precomputes them into a SQLite sidecar so each is a B-tree lookup.

**The index is derived state and is never authoritative.** Every fact in it comes from the
`.chrono`, which is the only source of truth. That single rule buys a great deal:

* it can be **deleted** at any time -- the worst outcome is a rebuild;
* it can be **rebuilt** from the recording alone, byte-for-byte identical;
* it can be **wrong about nothing**, because a stale one is detected (the recording's
  fingerprint and the indexer's version are stamped into it) and discarded rather than
  trusted;
* durability can be relaxed to the floor -- there is nothing here that a crash can lose
  that a rebuild cannot recreate.

If a query cannot be answered from the index, the honest fallback is to read the
recording, slowly. If a query *disagrees* with the recording, the index is wrong by
definition.

What this layer owns
--------------------
The schema, the indexers that populate it, and the staleness rule. It reads a recording
through `store`'s typed surface and writes SQLite; it computes nothing that the events do
not already contain.

What it must never know
-----------------------
Anything above it -- `reconstruct`, `query`, `server` (the dependency arrow points down).
It also never *interprets*: it does not decide what a query means or how a result is
rendered. It stores `seq` numbers, and `seq` is the address every layer above already
speaks. In particular it stores **pointers, not events**: the events stay in the
`.chrono`, which is what keeps the index a few bytes per event instead of a second copy of
the recording.

Design: [ADR-0008](../../../docs/adr/0008-index-schema.md). Built day 26.
"""
