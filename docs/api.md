# The ChronoTrace HTTP API

The browser UI (Phase 5) talks to a small local HTTP server over this contract. The full
design — every endpoint's screen, query, latency budget, and cache policy — is
[ADR-0010](adr/0010-api-contract.md); the scope the API serves is
[ADR-0011](adr/0011-phase5-scope.md).

## The contract is the code

The wire types live in [`src/chronotrace/server/dto.py`](../src/chronotrace/server/dto.py) as
plain dataclasses, and the OpenAPI spec is **generated from them** at runtime (day 33) — served
at `GET /openapi.json`, with interactive docs at `/docs`. There is no hand-written spec to
drift: the types are the single source of truth.

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
| `WS` | `/api/sessions/{id}/stream` | live events while recording (day 34) |

## Caching, errors, security — the short version

- **Immutable recordings cache forever.** A finished `.chrono` never changes, so read responses
  carry a strong `ETag` and `Cache-Control: immutable`. A still-recording session is `no-store`.
- **Errors are problem-details.** A `Problem` body carries a `code` and `status`, each mapped to
  a distinct user action (re-record, upgrade, build the index, pick another seq). The mapping is
  `dto.STATUS_FOR`, tested exhaustive.
- **Localhost only.** The server binds `127.0.0.1` and has no auth, because a recording is
  program memory (secrets) and there is no network exposure to authenticate. `--host` warns.
