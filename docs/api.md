# The ChronoTrace HTTP API

The browser UI (Phase 5) talks to a small local HTTP server over this contract. The full
design — every endpoint's screen, query, latency budget, and cache policy — is
[ADR-0010](adr/0010-api-contract.md); the scope the API serves is
[ADR-0011](adr/0011-phase5-scope.md).

## Running it

The server is a FastAPI app behind the optional `[ui]` extra, so a user who only records
installs no web dependencies:

```bash
pip install -e ".[ui]"
chronotrace serve --dir .        # --host/--port/--ui-origin available; binds 127.0.0.1
```

`create_app(config)` is a factory (no module-level globals, no import-time I/O), and the
recording resources — one mmap and one index connection per session — are opened lazily and
closed together on shutdown by the app's lifespan. Opening once per session, not once per
request, is what makes a playhead drag cheap and keeps Windows from blocking the index's own
rebuild (issue #10).

## The contract is the code

The wire types live in [`src/chronotrace/server/dto.py`](../src/chronotrace/server/dto.py) as
plain dataclasses, and the OpenAPI spec is **generated from them** — served at
`GET /openapi.json`, with interactive docs at `/docs`. There is no hand-written spec to drift:
the types are the single source of truth.

One rule the layer enforces by test: **no storage type ever reaches the wire.** A response is
always a resolved DTO (`name -> "preview"`), never a `ProgramState`, `Delta`, or
`CapturedValue`. `tests/server/test_dto.py` walks the DTO type graph and fails if any storage
type is reachable.

## Endpoints (summary — see ADR-0010 for budgets and caching)

| method | path | returns |
|---|---|---|
| `GET` | `/api/sessions` | `SessionSummary[]` — the recordings on disk |
| `GET` | `/api/sessions/{id}` | `SessionMeta` — event count, `truncated`, `indexed`, format version |
| `GET` | `/api/sessions/{id}/timeline?buckets=N` | `Timeline` — the scrubber's density background |
| `GET` | `/api/sessions/{id}/state?seq=` | `State` — reconstructed frames + value previews (the hot one) |
| `GET` | `/api/sessions/{id}/value?ref=` | `Value` — one level of a variable, expanded on click |
| `GET` | `/api/sessions/{id}/source?file=` | `Source` — source text + per-line heatmap |
| `GET` | `/api/sessions/{id}/calltree?seq=` | `CallTree` — the frames live at an instant |
| `POST` | `/api/sessions/{id}/query` | `QueryResult` — a typed query by name + args |
| `GET` | `/api/sessions/{id}/step?seq=&dir=&mode=` | `StepResult` — the destination instant, or an edge |
| `WS` | `/api/sessions/{id}/stream` | live timeline while recording (day 34) |

## The live stream (`WS /api/sessions/{id}/stream`)

Watch a recording's timeline fill as the program runs, then scrub it the moment it ends. The
server tails the `.chrono` while it is still being written (`store.tailer`, which reuses the
crash-recovery classifier -- a torn tail is "not finished yet" in a live file, "corrupt" in a
finished one, and only whether the writer is alive tells them apart) and pushes one aggregated
`StreamFrame` per ~100 ms tick:

```jsonc
{ "total_events": 41000, "state": "running",
  "density":  [ { "first_seq": 40960, "event_count": 512 } ],  // new events per bucket
  "notable":  [ { "seq": 40987, "kind": "raise", "lineno": 42 } ],  // exceptions, live
  "dropped":  0 }                                              // backpressure summary
```

- **It streams shape, not events.** Density buckets plus notable events (exceptions), never the
  raw stream -- the per-instant detail is a `/state` call away once the playhead parks, exactly
  as the day-27 density index trades the event stream for a per-bucket count.
- **Batched by time.** One frame per tick, so a 100k-event/sec burst is one bounded frame, not
  100k messages that would drown the browser; a slow program still updates within a tick because
  the writer flushes partial blocks on an interval.
- **Drop-and-summarise backpressure.** A frame's size is bounded by policy (`density` is compact,
  `notable` is capped); anything beyond becomes a `dropped` count, so server memory stays flat
  under a slow client. This drops *frames from a live preview*, never *data from the recording* --
  the file stays complete on disk, and the distinction is the whole design.
- **`state` ends the stream.** `complete` (the footer arrived -- now fully scrubbable) or
  `truncated` (events dropped, or the writer died before a footer). Then the socket closes.
- **`Origin` is validated by hand.** Browsers do not apply CORS to WebSocket handshakes, so this
  endpoint checks `Origin` against the UI allowlist explicitly -- the hole most local dev tools
  leave open. A page on any other origin is refused before the handshake completes.

`chronotrace record --ui script.py` records live: it serves the recording as it streams (a
separate process, isolated from the monitored one) and opens a browser. (The flags precede the
script — `argparse.REMAINDER` passes everything after it to the target as its own `argv`.) The visual scrubber
lands with the day-35 UI; until then `--ui` opens the API explorer at `/docs`.

## Caching model

A finished `.chrono` is append-only and never mutated, so a given `seq`/`file`'s answer is the
same bytes forever. Every immutable read endpoint (`meta`, `timeline`, `state`, `source`,
`calltree`, `value`, `step`) carries a strong `ETag` — `"<recording-fingerprint>-<key>"`, where
the fingerprint is the cheap day-25 content hash — and `Cache-Control: public, max-age=31536000,
immutable`. A repeat request with a matching `If-None-Match` is a `304` the server answers
without reconstructing anything. Correctness is free because nothing is ever invalidated.

The two endpoints that are *not* cached say so: `GET /api/sessions` is `no-store` (the directory
changes under it) and `POST .../query` is `no-store` (it has a request body). A still-recording
session's live state will be `no-store` too — that path arrives with the day-34 stream.

`/state` is the hot endpoint and is built for a drag: it ships value *previews* (expanded lazily
via `/value?ref=`), runs the blocking reconstruction in a threadpool, and **cancels on
disconnect** — the browser aborts the fetch for every instant the playhead has left, and the
handler checks `request.is_disconnected()` before doing the work, so stale requests cost nothing.

## Errors and security — the short version

- **Errors are problem-details.** A `Problem` body carries a `code` and `status`, each mapped to
  a distinct user action (re-record, upgrade, build the index, pick another seq). The mapping is
  `dto.STATUS_FOR`, tested exhaustive; `errors.py` maps the day-13 exception hierarchy onto it.
- **Localhost is not a boundary.** The server binds `127.0.0.1` and has no auth, because a
  recording is program memory (secrets) and there is no network to authenticate against — but a
  web page on any origin can reach `127.0.0.1`. So the server also **validates the `Host` header**
  (against DNS rebinding), **locks CORS** to the configured UI origin, and **contains session
  ids** to the recordings directory (no `..` traversal). `--host` is honoured but warns loudly.
