# ADR-0010: The HTTP contract between the engine and the browser

**Status:** accepted · **Date:** day 32 · **Context:** Phase 5 makes the engine *seeable*. The
UI needs an API. Get this wrong and either the scrubber is slow, or the on-disk format becomes
the wire format becomes the public API — welded together forever.

## Decision 1 — DTOs, never storage types (the most important one)

`server/` serialises the explicit wire types in `server/dto.py` and **nothing else**. A
`ProgramState`, `Delta`, `CapturedValue`, `Event` or `Keyframe` never crosses the wire.

The cost of leaking is concrete and permanent: if `/state` returned a `ProgramState`, then the
reconstruct layer's DTO would be the JSON the browser parses, which would be the public API —
and the day we wanted to add a field to `ProgramState`, or change how a keyframe stores
bindings, we would break every client. Three things that must stay free to change (the on-disk
format, the reconstructed state object, the wire shape) would be one thing that cannot.

So a DTO is the **resolved, display-ready** form of an engine answer. Where a `ProgramState`
carries `name_id -> value_ref`, a `Frame` carries `name -> preview string`; the id→text
resolution happens *at this boundary and never leaks past it*. This is the same dependency
arrow the project has enforced for 31 days, at its most dangerous edge — and it is enforced
the same way: by a test. `test_dto.py::test_no_storage_type_can_reach_the_wire` walks every
DTO's type graph and fails if any leaf is not a primitive, an enum, or another DTO. It is an
allowlist, so a storage type nobody thought to forbid is forbidden by default.

## Decision 2 — endpoints derived from the five screens, not the engine

An API designed from what the engine *can do* leaks the engine; one designed from what the
screens *render* stays small. Every endpoint maps to one screen, the query it drives, and a
latency budget (a design without budgets is not done).

| endpoint | screen | maps to | budget | cache |
|---|---|---|---:|---|
| `GET /api/sessions` | session picker | list `.chrono` files | 50 ms | `no-store` (dir changes) |
| `GET /api/sessions/{id}` | header | open + `META` | 30 ms | `ETag`, immutable |
| `GET .../timeline?buckets=N` | scrubber background | day-27 density | 30 ms | `ETag`, immutable |
| `GET .../state?seq=` | **the scrubber (hot)** | `reconstruct(seq)` + resolve | **< 50 ms p95** | `ETag`, immutable |
| `GET .../source?file=` | source + heatmap | read file + `heatmap` | 40 ms | `ETag`, immutable |
| `GET .../calltree?seq=` | call tree (stack) | `stack_at` | 30 ms | `ETag`, immutable |
| `GET .../calltree/children?parent=` | call tree (lazy) | `child_frames` (day 38) | 30 ms | `ETag`, immutable |
| `GET .../diff?seq=` | variables (day 37) | day-16 deltas at `seq` | 30 ms | `ETag`, immutable |
| `POST .../query` | query box | day-28 registry | per query's contract | `no-store` (has a body) |
| `GET /api/queries` | query form (day 38) | registry descriptors | 20 ms | (static per build) |
| `GET .../step?seq=&dir=&mode=` | step buttons | day-21 stepping | < 20 ms | `ETag`, immutable |
| `GET .../value?ref=` | expand a variable | resolve one ref, one level | 20 ms | `ETag`, immutable |
| `WS .../stream` | live tail (day 34) | monitoring feed | — | n/a |

*Reconciled at Checkpoint 5 (day 39): `/diff`, `/calltree/children` and `/api/queries` were added
within scope after this ADR was written (days 37–38); all are reads or descriptors. The generated
`/openapi.json` (decision 7) is the authoritative, drift-proof list — this table is illustrative.*

## Decision 3 — `/state` is designed for a drag

Dragging the playhead fires `/state` continuously, so it is built to survive that:

- **Previews, not values.** A `Frame` ships each local as a `preview` string plus a `ref` and
  `has_children`. A frame with a 10,000-element list ships one short preview, not 10,000
  elements. Expansion is a *separate* click → `/value?ref=`, one level at a time. This is not a
  new mechanism: it is day-20 lazy resolution (`ValueResolver` resolves the ref actually asked
  for, nothing else) exposed over HTTP.
- **Debounce on the client**, cancel on the server. The browser debounces the drag; a
  request the user has scrubbed past is abandoned by disconnecting, and the handler checks for
  disconnect the way the indexer checks its cancel signal. No speculative work outlives the
  question.
- **Small and cacheable**, see below.

## Decision 4 — immutable recordings make caching trivially correct

A finished `.chrono` is append-only and never mutated (day-11 decision, paying off again), so
`/state?seq=42` for it returns the same bytes forever. Every read endpoint on a finished
recording gets a **strong `ETag`** (derived from the day-25 recording fingerprint + the seq/args)
and `Cache-Control: public, max-age=31536000, immutable`. The browser and any proxy cache it
permanently; correctness is free because the thing cannot change.

A **still-recording** session (day 34) is the exception: its state at a high seq is not yet
determined, so those responses are `Cache-Control: no-store`. The immutability that makes
caching trivial is exactly what a live session lacks, and the header says so.

## Decision 5 — errors are problem-details mapped to distinct actions

Errors are an RFC-7807-ish `Problem` body: a machine `code`, an HTTP `status`, and a human
`detail`. The day-13 exception hierarchy maps to codes, and the *only reason to distinguish
them is that the user does something different about each*:

| code | status | what the user does |
|---|---:|---|
| `corrupt` | 422 | re-record — the file is damaged |
| `unsupported_version` | 422 | upgrade ChronoTrace |
| `not_indexed` | 409 | build the index (or retry after the lazy build) |
| `seq_out_of_range` | 404 | pick a seq in `[0, event_count)` |
| `truncated_seq` | 404 | this instant was lost to a crash; the recording ends earlier |
| `not_found` | 404 | no such session or file |
| `unknown_query` | 400 | typo in the query name |
| `bad_request` | 400 | malformed request or query args — incl. a bad `--if` condition (day 38), whose `detail` carries the parser's column / the rule a forbidden construct broke |

The mapping is `dto.STATUS_FOR`, tested exhaustive, so a new code cannot ship without a status.
`not_indexed` is consistent with ADR-0008's lazy fallback: the server may build the index on
first request (reporting progress over the WebSocket) or return 409 with the action — a choice
the day-33 implementation makes, not the contract.

## Decision 6 — localhost-only, as a threat model, not a default

The server binds `127.0.0.1`. **No auth, because there is no network exposure to authenticate
against.** The threat model (day 47 formalises it): a recording is a snapshot of program
memory — it contains whatever was in variables, i.e. secrets. A debugger that bound `0.0.0.0`
by default would publish those to every machine on the LAN the moment it started. Binding
loopback makes that structurally impossible. `--host` is honoured but warns loudly, because
someone who types it has chosen to accept the exposure; someone who did not should never be
surprised by it.

## Decision 7 — OpenAPI generated from types, never hand-written

The spec is generated from the DTOs at runtime (FastAPI, day 33, `/openapi.json`). A
hand-written spec drifts from the code the first time someone forgets to update it; a generated
one *is* the code, so it cannot. This is why the DTOs are the contract and this ADR is prose
about them, not a second source of truth to keep in sync.

## Consequences

The wire contract is small (ten endpoints, derived from five screens), framework-free (stdlib
dataclasses + `to_wire`, so FastAPI is transport not contract), and enforced by tests (no
storage leak, exhaustive error mapping, golden shapes). The engine stays free to change behind
it. **Reversal trigger:** Decision 3's preview-only `/state` if a profile shows the extra
`/value` round-trips dominate a real UI's latency — then batch the first expansion level into
`/state`, still lazily beyond it.
