# ADR-0008: The index — schema, timing, and why it is a SQLite sidecar

- **Status:** Accepted (design; implementation days 26–27)
- **Date:** 2026-07-22 (day 25 of 50)
- **Deciders:** dharmppp21
- **Builds on:** ADR-0004 (`.chrono` format), ADR-0006 (reconstruction)

## Context

Reconstruction answers *"what was the state at instant S?"* — one instant at a time. That
is the wrong shape for the questions people actually ask while debugging, which are about
the **whole timeline at once**:

> *Who last wrote to `total`?* · *Where did this exception originate?* · *Every time this
> line ran, what was `x`?*

Answering those by replaying is O(events) per question. This is the phase that makes them
lookups, and it is ChronoTrace's novel contribution: recording gives you the past, the
index makes the past **queryable**.

## 1. The queries, enumerated before the schema

Designing an index for hypothetical queries is speculative generality with a storage bill,
and this bill is real (§6: three times the recording). So the list comes first, and
**every index below traces to a row in it**:

| # | query | who asks | powers |
|---|---|---|---|
| Q1 | every write to variable `x` | user | "show me every mutation of `total`" |
| Q2 | **the last write to `x` before `seq`** | query engine | day 29 provenance — the demo |
| Q3 | every hit of `file:line` | user | day 30 retroactive breakpoints |
| Q4 | the children of frame F | UI | the call tree, one level at a time |
| Q5 | every invocation of function F | user | "who called this?" |
| Q6 | every exception of type T, and each origin | user | day 29 "where did this come from?" |
| Q7 | events per time bucket | UI | the scrubber's background |
| Q8 | text → id ("total" → `name_id`) | every query above | the user types names, the index stores ids |

## 2. The hard decision: *when* does indexing happen?

### (a) During recording — **rejected**

The index would be ready the instant the program exits. It is still wrong, for the reason
Phase 1 spent six days on: **it puts work on the user's hot path**. Recording is
already 6.7× on control flow, and every microsecond there is paid once per line of
somebody's program. Adding a SQLite insert per event would dominate that budget.

It also cannot work as stated. `frames.parent_frame_id` needs the call structure and
`density` needs the recording's full time range — neither is known until the end. Indexing
during recording would mean buffering most of it anyway, which is the "after" option with
the hot-path cost added.

### (b) After recording, at close — **chosen**

A pass over the finished event stream. Measured: **648,000 events/sec**, so a 281k-event
recording indexes in **0.43 s** and a million-event recording in about 1.5 s. Predictable,
off the hot path, and the index is ready before the user opens anything.

### (c) Lazily, on first query — **chosen as the fallback**

Zero cost until someone queries, at the price of a stall the first time they click. As the
*default* that is a bad trade — the first click is the one that forms an impression. As a
**fallback it is mandatory**, because of the next paragraph.

### The consequence, stated rather than apologised for

A crash-truncated recording never ran its close pass, so **it has no index**. And a
crashed recording is exactly the one most worth querying.

This composes cleanly with day 17's recovery rather than fighting it. `ChronoReader`
already opens a truncated recording by scanning intact blocks and reports
`reader.truncated`; the index builder consumes that same recovered prefix. So:

- clean close → index built eagerly, ready on open;
- crash → no index, and the **first query builds one from the recovered prefix**, stamped
  with that prefix's fingerprint.

The user waits once, on the recording where they are already grateful to have anything.
The alternative — refusing to query a crashed recording — would fail exactly when the tool
matters most.

## 3. SQLite, not a hand-rolled index

**Chosen: SQLite.** It is in the standard library (the day-1 zero-dependency rule holds),
it has a real query planner, and it has spent twenty-five years getting B-trees,
durability, concurrency and corruption-resistance right.

The counter-argument is real: a purpose-built columnar index would be smaller and probably
faster. `var_writes` is two integers per row and SQLite spends 14.5 bytes on it.

**It is still the wrong thing to build today.** Hand-rolling a B-tree here means
reinventing infrastructure that is *already in the process*, and then owning its bugs
forever — in a project whose entire pitch is that its correctness is provable. Every hour
spent on a page cache is an hour not spent on the queries that are the actual contribution.

> Knowing when **not** to build is the judgment being exercised. The senior move is not
> writing the B-tree; it is noticing that the B-tree is not the product.

**Reversal trigger:** if day-40 profiling shows index *lookup* (not the query engine
around it) on the critical path, or if the size ratio in §6 blocks a real recording.
Phase 6 owns that, and only with a profile behind it.

## 4. Sidecar, not embedded

The index lives **beside** the recording: `recording.chrono.idx`.

- The `.chrono` stays **append-only and immutable** — a day-11 property that reconstruction
  depends on (a cached `ProgramState` can never go stale because the file cannot change).
  Writing an index into it would mean mutating a finished recording.
- The index is a **derived cache**: deletable, rebuildable, and safe to throw away on any
  doubt. That is only true if it is a separate file.
- SQLite wants its own file. Embedding it would mean re-implementing a VFS.

**Consequence, accepted:** sharing a recording means sharing one file, and the index
rebuilds on the other end. That is the right trade — the alternative is shipping a
derived, machine-specific, possibly-stale cache to someone else and hoping they trust it.

**Read-only directory:** if the recording's directory is not writable, the sidecar goes to
a user cache directory (`%LOCALAPPDATA%`/`$XDG_CACHE_HOME`/`~/.cache`) keyed by the
recording's fingerprint. Reading someone else's recording out of a read-only share is
ordinary, and it must not be a hard failure.

**Concurrent builders:** two processes may index the same recording at once. Each builds
to a temporary file and `os.replace`s it into position — the same atomic-swap discipline
day 17's `repair` already uses. The loser's work is wasted, never corrupt. Locking was
rejected: it adds a failure mode (a stale lock blocks every future query) to avoid a cost
that is a few seconds of duplicated work.

## 5. The schema

Tables: `meta`, `strings`, `codes`, `var_writes`, `line_hits`, `frames`, `exceptions`,
`density`. DDL in [`src/chronotrace/index/schema.py`](../../src/chronotrace/index/schema.py),
which is the single source of truth; this is the justification.

| index | serves | complexity |
|---|---|---|
| `var_writes` PK `(name_id, seq)` | **Q1**, **Q2** | Q2 is O(log n) — a covering-index seek, measured **37 µs** |
| `line_hits` PK `(file_id, lineno, seq)` | **Q3** | O(log n + hits) |
| `frames` PK `(frame_id)` | frame → its row | O(log n) |
| `ix_frames_parent` `(parent_frame_id, entry_seq)` | **Q4** | O(log n + children) |
| `ix_frames_code` `(code_id, entry_seq)` | **Q5** | O(log n + calls) |
| `exceptions` PK `(seq)` | origin walk via `cause_seq` | O(log n) per hop |
| `ix_exceptions_type` `(type_id, seq)` | **Q6** | O(log n + hits) |
| `density` PK `(bucket)` | **Q7** | full scan of a fixed tiny table |
| `strings` PK `(id)` | id → text, for rendering | O(log n) |
| `ix_strings_text` `(text)` | **Q8** | O(log n) |
| `codes` PK `(code_id)` | code → file/qualname/line | O(log n) |

### One index was deleted today, which is the rule working

`ix_var_frame (frame_id, seq)` — "every write in *this* frame" — was drafted and cut. It
has no query in §1: scoping Q1 to a frame is a filter over the rows for one name, which is
small. It would have cost a whole extra B-tree on the largest table to serve a query
nobody has asked for. **An index whose query nobody wrote down gets deleted, and
`test_every_declared_index_is_justified_by_a_named_query` parses this document to enforce
it** — adding an index without adding its row here fails the build.

`ix_codes_file` was cut for the same reason: Q3 is answered by `line_hits` directly, so
"the code objects in a file" served nothing.

### `parent_frame_id` lives here, and only here

Day 20 removed `parent_id` from `ProgramState` because a frame that entered *before* a
keyframe has no recoverable parent from that keyframe alone — it was path-dependent, and
state must be a pure function of `seq`. ADR-0006 named this index as the authority
instead. A single forward pass over the events knows every parent exactly, which is
precisely the shape an offline index can do and an online reconstruction cannot.

## 6. Size, measured, including the number that hurts

`json_pipeline`, 281,019 events:

| | bytes/event | vs. the recording |
|---|---:|---:|
| the `.chrono` itself | 4.94 | 1.0× |
| index, rowid tables + separate indexes | 30.3 | 6.3× |
| **index, `WITHOUT ROWID` clustered** | **14.5** | **3.0×** |

`WITHOUT ROWID` halves it, with identical build time and identical query latency, because
these tables are only ever reached through their composite key — an implicit `rowid` is a
second key nobody queries and a second B-tree to store.

**The index is three times the size of the recording it indexes**, and that is not a bug
to fix: the `.chrono` is columnar and zstd-compressed at 4.94 B/event, while a B-tree of
pointers is uncompressed with per-row overhead. Storing pointers costs more than storing
the compressed data they point into. (The index stores **pointers, not events** — the
events stay in the `.chrono`. Otherwise it would be far worse than 3×.)

**The 10 GB case, honestly:** ~2.02 billion events → a **~29 GB index** taking **~52
minutes** to build. That is not acceptable and it is not solved today. The design response
is a size threshold above which indexing is not automatic — the user is told the recording
is large and asked, rather than surprised by an hour of disk activity. The threshold is a
knob, like day 15's keyframe interval, and its default needs the same grid measurement
ADR-0005 did. Tracked; day 26 ships the knob, day 31's checkpoint tunes it.

## 7. The index cannot be built without a change to the format

Q8 — text → id — is required by every other query, because the user types `total` and the
index stores `name_id`. At index time the recorder is long gone and **the `.chrono` does
not persist its intern tables** ([#6](https://github.com/dharmppp21/ChronoTrace/issues/6)).

The alternative was to build the index at close, while the recorder is still alive, and
take the tables directly from it. **Rejected**, and the reason is the rule in §4: an index
must be rebuildable from the recording *alone*. Take the strings from a live recorder and
a deleted index can never be rebuilt, and a crash-truncated recording — the one §2 just
promised to index lazily — has neither strings nor index.

So the format grows a `STRINGS` block (the type is already reserved as `0x0002` in
ADR-0004, unused since day 11), carrying names, exception type names, and for code objects
`(filename, qualname, first_lineno)` — never anything requiring the original `.pyc`.
Optional block, minor bump to **1.6**, implemented day 26 as the indexer's first
prerequisite.

This is the design day earning its keep: the requirement was discovered on paper, before
an indexer was written against a format that could not feed it.

## 8. Rebuild and staleness

The index is derived state, so there is never a migration — only a rebuild. `meta` stamps
four things, and any mismatch discards the whole file:

| stamped | catches |
|---|---|
| `schema_version` | the DDL changed |
| `indexer_version` | the *output* changed for unchanged input (a fixed bug, a new column) |
| `recording_fingerprint` | a different recording at the same path |
| `event_count` | provenance for the log line |

Two versions rather than one because they drift independently: the same tables filled in
*more correctly* still means every existing index is wrong.

The fingerprint hashes the **header, the trailing 32 bytes, and the size** — O(1), not
O(size). This check runs before every query; hashing 10 GB would cost more than the
queries it guards. The tail is the EOCD, which carries the INDEX block's offset, length
and CRC, so it changes whenever any block does. A deliberately crafted collision is
possible and out of scope: this is cache validity, not a security boundary.

**A missing or half-written index counts as stale**, which is what an interrupted build
looks like with durability off — and the answer to that is a rebuild, not a repair.
Durability *is* off (`journal_mode=OFF`, `synchronous=OFF`): paying for it on a cache is
paying for the wrong thing, and the recording it derives from is untouched either way.

## Consequences

**Buys:** the questions debugging is actually made of, as B-tree lookups instead of
replays. The demo query at 37 µs. An index that can always be deleted, and is never
trusted when it should not be.

**Costs:** an index three times the size of its recording; a second file to keep beside
the first; a format bump to make the whole thing rebuildable; and an unsolved 10 GB case
that gets a knob and a warning rather than a silent hour of disk I/O.

**Reversal trigger:** §3 (SQLite) if a profile puts lookup on the critical path; §2 (eager
at close) if measured close-time indexing annoys users more than a first-query stall would
— which is a question only real use can answer.
