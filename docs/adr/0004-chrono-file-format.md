# ADR-0004: A purpose-built columnar `.chrono` format

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** dharmppp21
- **Supersedes / Superseded by:** none

## Context

[ADR-0001](0001-recording-strategy.md) committed to omniscient recording, which
makes recordings large and forces Phase 2 to compress hard. Day 10 measured the
consequence: **1M events cost 225 MB live in RAM**, so the store is not an
optimisation — it is what makes recordings survive past the length of a toy
program. And the store's format, once a recording exists in someone's bug report,
is a compatibility contract that is expensive to change.

The requirements are specific and partly in tension: append-only writes at
recorder speed; **random access by `seq`** (the whole product); crash tolerance
(`kill -9` mid-write must leave a readable prefix); high compression;
mmap-ability; forward compatibility; and **safety on open** — a `.chrono` from a
stranger must never execute code.

## Decision

**A purpose-built binary format:** a fixed header, a sequence of length-and-CRC
framed blocks, and an index written last whose presence signals a clean close.
Events are stored **column-major** in blocks of N (default 65536); values live in
a content-addressed pool; strings are interned. All integers little-endian; values
are msgpack from a closed type registry, never pickle. The full byte layout is
[`docs/format-spec.md`](../format-spec.md).

## Alternatives considered

### Row-oriented records (msgpack per event)

One self-describing record per event, concatenated. Simple, and better for reading
a single event in isolation. **Rejected on a measured number.** Compressing a real
event stream (zlib as a zstd proxy; the *ratio* is what matters):

| workload | row B/event | **columnar B/event** | win |
|---|---:|---:|---:|
| tight_loop | 6.44 | **0.54** | 11.9× |
| json_pipeline (realistic) | 8.56 | **1.19** | 7.2× |
| fib_recursive | 8.51 | **0.93** | 9.2× |

Columnar is **7–12× smaller** because it puts like fields next to like: `seq` is
`+1` almost everywhere (delta-of-delta → runs of zero), `kind` is a handful of
values (run-length), `code_id`/`frame_id` are locally constant. Row storage
interleaves them and hands the compressor noise. Columnar is also what makes the
timeline-density query (day 27) cheap — count events per time bucket by scanning
one column, not deserialising every record. The cost is real and accepted: reading
*one* event means touching every column of its block, and a point read must decode
a whole block. We almost never read one event; we scan ranges and reconstruct
state, which is exactly what columnar is good at.

### SQLite as the event store

Random access, transactions and a query language for free. **Rejected as the raw
event log:** it is a row store (defeating the columnar compression above), its
write path is not built for append-at-recorder-speed sequential logging, and its
crash/durability model is heavier than a debug artifact needs. It remains the
right tool one layer up — day 27's *index* over the store is SQLite. Store and
index are different jobs.

### `pickle` for values

Serialises any Python object in one call. **Rejected, permanently, at the spec
level.** A recording is untrusted input shared in bug reports, and a single pickle
`__reduce__` turns "open this file" into "run this code" — an 163-byte proof of
this was built on day 3. Values are msgpack from a closed type registry; opening a
file constructs no arbitrary objects.

### Index at the start of the file

O(1) open with no scan. **Rejected structurally:** an append-only writer does not
know a block's offset until it has written it, so the index cannot precede the
data. It goes at the end, written last — and its *absence* becomes the free signal
that the writer died mid-recording, which the reader handles by scanning framed
blocks and recovering the valid prefix.

### `fsync` every block

Guarantees each event is on the platter. **Rejected:** it would gate the traced
program on disk latency and could halve its speed, to protect a *debug artifact*
whose last few events nobody will miss after a crash. Durability comes from framing
(any block that reached disk is self-validating), not from `fsync`; the writer
`fsync`s once, at clean close.

## Consequences

**What this buys us:** recordings ~7–12× smaller than the naive encoding before
zstd even runs; random access by `seq` via the index; crash tolerance by
construction; mmap-ability (fixed frames, u64 offsets); forward compatibility
(optional blocks + minor versions); and a safe-to-open guarantee.

**What this costs us:** a format we must maintain and version, and a point-read
penalty (decode a whole block for one event) that we pay knowingly because it is
not our access pattern. A default block size of 65536 events is a guess at the
compression-vs-random-access tension — bigger compresses better and shrinks the
index but makes a point read decode more — and **day 18 is the experiment that
validates or moves it**, not a value pulled from the air.

**What this forces later:** day 12 writes the framing and the columnar codecs; day
14 adds zstd (the `COMPRESSED_ZSTD` block flag and the msgpack value registry are
reserved for it now); day 15 adds the optional `KEYFRAMES` block; day 18 measures
real file sizes and settles the block size.

**Reversal trigger:** revisit the columnar decision only if day-18 file sizes come
in within ~1.5× of row encoding on realistic recordings (they will not, per the
table above), or if a point-read-heavy access pattern emerges that the query layer
cannot satisfy from the index. The format-*version* mechanism means most changes
are additive and need no ADR; only a `version_major` bump — an incompatible
reframing — would.
