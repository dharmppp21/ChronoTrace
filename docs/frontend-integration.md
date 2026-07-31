# Frontend ↔ backend integration contract

The Phase 5 UI lives in `frontend/` and talks to the server **only** over the HTTP contract
(the generated OpenAPI client — never a hand-written `fetch`). This file is the seam between the
two: what the backend gives the frontend, and what the frontend must do so the backend can serve
and ship it. The engine (`src/chronotrace/`) is the Claude-owned side; `frontend/` is built in
Antigravity. Keep this file in sync when either side of the seam moves.

## Two modes, and why

- **Development:** `frontend/` runs on Vite's own dev server (HMR) and *proxies* the API to a
  running `chronotrace serve`. The browser only ever talks to Vite's origin, so there is no
  CORS in dev — the proxy makes it same-origin.
- **Production:** one Python process serves the pre-built SPA from `chronotrace/_ui/` plus the
  API. **No Node at runtime** — a debugger you needed Node just to *view* a recording with would
  be a worse debugger. This is why the build output is bundled into the package, not served by a
  separate web server.

`server/app.py::_mount_ui` mounts `chronotrace/_ui/` at `/` when it exists, *after* the API
routes, so `/api/...`, `/openapi.json`, `/docs` and the WebSocket always win over the SPA
catch-all. When `_ui/` is absent (a source checkout with no build) the server is API-only — no
crash.

## What the frontend must satisfy (the contract CI and packaging enforce)

1. **Build output → `src/chronotrace/_ui/`.** Set Vite's `build.outDir` to `../src/chronotrace/_ui`
   with `emptyOutDir: true`. That directory is gitignored (a build artifact) and bundled into the
   wheel by `pyproject.toml`'s `artifacts = ["src/chronotrace/_ui/**"]`. Build elsewhere and the
   server serves nothing and the wheel ships nothing.
2. **`package.json` scripts:** `typecheck`, `test`, `build`. The `frontend` job in
   `.github/workflows/ci.yml` runs exactly these (`npm ci && npm run typecheck && npm test &&
   npm run build`); it is guarded on `frontend/` existing, so it is green until the UI lands and
   enforcing from the first commit after.
3. **Generate the API client from `/openapi.json`** (e.g. `openapi-typescript`), do not hand-write
   requests. A backend DTO change then breaks the frontend *build*, not the demo — that is the
   whole point of the day-32 typed contract. Regenerate whenever the DTOs change.

## Dev setup (run the API, proxy to it)

```bash
# terminal 1 — the API over some recordings, allowing the Vite origin so the WS Origin check passes
chronotrace serve --dir ./recordings --ui-origin http://localhost:5173

# terminal 2 — the UI with HMR, proxying /api, /openapi.json and the WS to the API
cd frontend && npm run dev
```

Vite's proxy must forward `/api` **and** upgrade the WebSocket (`ws: true`) for
`/api/sessions/{id}/stream`. The proxied WS carries `Origin: http://localhost:5173`, which is why
the server is started with `--ui-origin` matching it — the WS validates `Origin` by hand because
browsers do not apply CORS to WebSocket handshakes.

## Source panel (day 36) — the backend contract

`GET /api/sessions/{id}/source?file=<name>` returns `Source { file, lines, heatmap, available }`:

- **`lines`** is the file on disk *now* (the recording stores only a hash, not the text), empty
  only when the file is gone since recording.
- **`heatmap`** is `[{ lineno, count }]` for the lines that ran — the day-27 line index. Counts
  are **raw**; the log scale and the on-hover raw count are the panel's job (a linear scale
  renders every line except the hottest loop as identically cold). A real code line **absent**
  from the heatmap is "recorded but never ran" — the panel knows it is real code because it has
  the source. *The never-taken branch is the panel's most valuable pixel.*
- **`available`** says whether the heatmap's line numbers align to `lines`. `True` → overlay it.
  `False` → the file changed since recording (or was never hashed): **show the current source but
  withhold the heatmap overlay + a "source changed" banner.** A wrong line is worse than no line.
  `lines` empty + `available: false` → the file is gone; show "source unavailable".

**Recorded vs not-recorded is a file-level signal:** a file not in the recording (out of scope,
day-9 filtering) returns **404 `not_found`** — render it as *not recorded*, distinct from a
recorded file's line that *never ran* (200, absent from the heatmap). Those are different facts.
*(A never-executed line leaves no trace, so within a partially-scoped file the two cannot be told
apart — a property of omniscient recording, not a gap; the file-level signal is the honest one.)*

**Current file + executing line** come from `/state`: `frames[].file` and `frames[].lineno` for
the current frame drive which file to fetch and which line to highlight.

**Gutter click → retroactive breakpoint** uses the existing query endpoint, no new backend:
`POST /api/sessions/{id}/query` with `{ "name": "break", "args": { "file": "<name>", "lineno": N } }`
returns a `QueryResult` whose `hits[].seq` are the jump-to instants. This is the panel's hero
interaction — a breakpoint set on a program that already finished.

## Variables panel (day 37) — the backend contract

**Current locals** come from `/state?seq=`: `frames[].variables[]` = `{ name, preview, ref,
has_children, truncated, obj_id }`. `ref` + `has_children` drive lazy expansion via `/value?ref=`
(one level at a time — do not fetch a 10k-element list to show one row).

**The diff — the hero.** `GET /api/sessions/{id}/diff?seq=` → `{ seq, changes:[{ frame_id, name,
kind, old, new }] }`, `kind ∈ {added, removed, modified}`. Paint added=green, removed=red,
modified=amber (`old → new`). It is a **server-side lookup from the day-16 invertible deltas**,
not a comparison of two reconstructed states — the deltas already carry each binding's old and
new ref, so it is O(changes), exact, and cheap. What it can and cannot show:
- **Content** changes (an object mutated, or rebound to different content) → MODIFIED, `old != new`.
- An **identity-only** change (rebound to a *different* object of equal content) is coalesced by
  day-8 content-addressed dedup — the refs are equal, no delta exists, **not shown**.
- **REMOVED** (`del x`) is rare: the recorder does not observe a name leaving `f_locals`.

**Honest markers (never silently empty).** The `preview` string already carries the lossy-capture
states rendered distinctly: `<redacted>`, `<budget>` (node budget hit), `<depth>` (depth limit),
`<cycle>` (a back-reference), a trailing `...` (a truncated container/string), `Type(...)` (an
opaque object). Match that fixed set and make each a hoverable, explained badge — never a blank
or `null`. The `truncated` flag is the structured signal that a container/string was cut short.
*(The exact "showing 100 of 1,000,000" count is not exposed yet — the preview shows `...` and
`truncated: true`; ask for a `length` field if you want the precise number, it is additive.)*

**Identity badges — a real limitation.** `obj_id` is the day-7 stable object id: two variables
sharing an `obj_id` are the same object, so badge the aliasing. **But `obj_id` is `null` for
`dict`/`list`** (they are not weakref-able, so no reuse-safe id can be assigned — issue #9), so
the badge covers **custom-object aliasing only**. The demo bug's shared *list* is not badge-able
until #9 is solved (which is hard: a stable id for a dict/list without retaining the object).

**Right-click → the query engine** (existing `POST /query`, no new backend): `{name:"var-writes",
args:{name}}` = every write to it; `{name:"provenance", args:{name, seq}}` = where its value came
from; `{name:"watch", args:{name}}` = every instant it changed. Each returns `hits[].seq` jumps.

## Production build

```bash
cd frontend && npm run build          # emits into ../src/chronotrace/_ui
pip wheel . --no-deps -w dist         # the wheel now carries the UI (day 44 automates this)
chronotrace serve --dir ./recordings  # one process serves API + UI at http://127.0.0.1:8000
```
