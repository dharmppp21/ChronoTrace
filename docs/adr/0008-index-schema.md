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

## Amendment (day 26, on building it)

Three corrections, all found by writing the thing rather than by rereading the design.

**1. `codes` stores text, not string ids.** §5 declared
`codes(code_id, file_id, qualname_id, ...)`, pointing into `strings`. That cannot work as
written: `strings.id` **is** the recording's `name_id`, because Q8 has to turn typed text
into the id `var_writes` stores, and `code_id` is a different id space. Sharing one pool
would need a second mapping table just to keep Q8 a direct lookup.

Interning them buys nothing anyway, and this is where the size argument actually applies:
`var_writes` has a row per variable write — millions — so storing a 4-byte `name_id`
instead of a repeated string is the whole game. `codes` has *hundreds* of rows. Interning
at that scale is ceremony. So `codes(code_id, filename, qualname, first_lineno)`, text
inline.

**2. `exceptions.type_id` still has nothing to resolve against.** Same id-space problem,
not yet solved because it has no consumer: the exceptions indexer lands on day 27, and its
`exc_types` table lands with it. Noted rather than built, so the table does not ship ahead
of anything that writes to it.

**3. The size estimate looked generous, and was not.** §6 predicted 14.5 B/event and 3.0×
the recording. Measured with `var_writes` alone: 5.4 B/event, 1.1×. That reading was the
*incomplete measurement*, not a wrong estimate — day 27 added `line_hits`, the largest
table, and the real figure is **17.1 B/event, 3.5×**, slightly worse than §6 predicted.
The 10 GB case is therefore ~34 GB rather than ~29 GB, and the threshold knob is still
needed.

**And one thing measurement overturned:** the standard "create indexes after bulk load"
advice does not hold for this schema — 4.49 s after vs 4.31 s before, with the clustered
`WITHOUT ROWID` form beating both at 3.68 s and half the size. The rows arrive in `seq`
order across a few hundred names, so every page is hot and maintaining the tree during
insert is nearly free. `var_writes` therefore ships **no secondary index at all**: the
primary key is the access path. Numbers in `benchmarks/RESULTS.md`.

## Amendment (day 27) — intervals encode *time*, not the tree

**Schema version 2.** Four more indexes shipped today, and one tempting optimisation was
tested and rejected.

### New tables and indexes, each with its query

| index | serves | complexity |
|---|---|---|
| `files` PK `(file_id)` + `ix_files_path` | a user names a file, the index stores ids | O(log n) |
| `exc_types` PK `(id)` | `type_id` → `"ValueError"`, the gap §7 left open | O(log n) |
| `ix_frames_entry` `(entry_seq, exit_seq)` | **"which frames were live at `seq`?"** — every scrub | O(log n + live) |

`files` exists because `line_hits` is the largest table in the index and carries a
`file_id` per row rather than a path. That is where interning pays; `codes` still stores
its qualname as text, for the reason the day-26 amendment gives.

### The insight the day was supposed to be about, and why it is wrong here

`entry_seq`/`exit_seq` look exactly like a nested-set encoding, which would make
"descendants of F" one indexed range scan instead of a recursive walk — *a descendant is
any frame whose interval nests inside F's*. Reusing data you already have instead of
adding a second mechanism is normally the right instinct.

**It does not hold, and generators are why** — the same feature that killed the stack
model in ADR-0002. Measured on `examples/generators.py::interleaved_generators`:

```
frame 1  [ 0, 19)  parent=None     <- the caller
frame 2  [ 3, 25)  parent=1        <- a generator: OUTLIVES its parent
frame 3  [ 7, 22)  parent=1        <- and OVERLAPS its sibling
```

Both invariants a nested-set encoding needs are violated. A child's interval is not
contained in its parent's, because the generators are finalised after the caller returned;
and siblings overlap, because two generators of the same function are alive at once. The
error runs the other way too: while a generator is suspended, every unrelated frame that
runs has an interval nesting *inside* it, so containment reports strangers as descendants.

So the two questions are split by what they actually are:

- **liveness is a time question** → the interval predicate,
  `entry_seq <= S AND (exit_seq > S OR exit_seq IS NULL)`, one indexed range scan. This
  *is* correct, and it handles a never-exiting frame for free.
- **ancestry is a structure question** → the `parent_frame_id` walk, as a recursive CTE.
  O(subtree), bounded by the subtree rather than the recording.

`test_descendants_match_the_recursive_cte_oracle` shows the two agree on a plain call
tree — which is exactly why the trap is easy to fall into — and
`test_intervals_stop_encoding_ancestry_once_frames_suspend` pins the counter-example so
the optimisation is never "rediscovered".

### The interval convention, and *live* vs *executing*

Intervals are **half-open**, `[entry_seq, exit_seq)`. At `exit_seq` the frame is already
gone from reconstructed state, so a closed interval would report a dead frame as live for
one `seq` — a stale row in the call-stack panel on every return.

Two distinct notions, both of which the UI needs:

- **live** — the frame exists at `seq`, *including a suspended generator* holding real
  locals while sitting on no stack. This index answers it.
- **executing** — the one frame that ran the event at `seq`. That is
  `ProgramState.current_frame_id`; reconstruction already answers it.

At a `YIELD` instant they coincide (the suspending frame ran the event that suspended it),
which is worth knowing before writing an assertion about them.

### The heatmap is not materialised — today

"How many times did each line run?" is a `GROUP BY` over `line_hits`. Measured at **12.8
ms** over 178,914 rows, which is acceptable for a background drawn once per file opened
and *not* acceptable for one recomputed while scrolling — it is over a frame budget, and
at 10M events it extrapolates to about half a second.

Not built today: nothing consumes it before day 35, and a second table is a second
representation of one fact with freedom to disagree. Tracked as issue #12 with the real
number and the breaking point, so the UI inherits a measurement instead of an assumption.

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
