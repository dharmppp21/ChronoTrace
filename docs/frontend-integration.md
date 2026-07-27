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

## Production build

```bash
cd frontend && npm run build          # emits into ../src/chronotrace/_ui
pip wheel . --no-deps -w dist         # the wheel now carries the UI (day 44 automates this)
chronotrace serve --dir ./recordings  # one process serves API + UI at http://127.0.0.1:8000
```
