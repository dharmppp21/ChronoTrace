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

## Call tree panel (day 38) — the backend contract

A normal debugger shows the call *stack*: the frames alive right now. ChronoTrace has the whole
recording, so it can show the call *tree* — **every call the program ever made** — with the
current stack highlighted inside it. That is the one-sentence superpower: click a function that
returned 200,000 events ago and *go there*. No live Python debugger can, because there is nothing
left to run.

Both a `CallFrame` node carries the same shape in either mode:
`{ frame_id, function, file, entry_seq, exit_seq, exit_kind, parent_frame_id }`.

- **`entry_seq`** is the call instant — clicking the node sets `currentSeq` to it (every panel then
  follows, because the UI is a pure function of `currentSeq`).
- **`exit_seq`** is where the frame left — the **jump-to-return** target, and with `entry_seq` the
  call's event span. `null` when the frame never returned.
- **`exit_kind` ∈ `{returned, raised, open}`.** `raised` is a frame CPython unwound because of an
  exception (day-6's UNWIND) — **colour it** (red); it is the one-glance answer to "where did it
  blow up?". `open` is a frame that never returned in the recording (`exit_seq` null): still live
  at the end, a suspended generator, or a crash-truncated tail — not an error, just not-yet-returned.
  Note the time-travel twist: even a frame *live at the current instant* carries its **eventual**
  `exit_kind`, so the tree can foretell that the frame you are paused in will be unwound.
- **`parent_frame_id`** is the caller — **jump to caller** navigates to that node (at this frame's
  `entry_seq`, the parent is the executing frame). `null` marks a forest root.

### Two modes

- **Stack mode** — `GET /api/sessions/{id}/calltree?seq=` → the frames **live at `seq`**, outermost
  first (`next_cursor` is always null; a stack is bounded by call depth). This is the familiar view
  users reach for. One indexed range query (day-27 `ix_frames_entry`).
- **Tree mode** — `GET /api/sessions/{id}/calltree/children?parent=<frame_id>&after=<entry_seq>` →
  one page of a frame's **direct children**, in call order. Omit `parent` to get the **forest roots**
  (where the tree bootstraps). `next_cursor` is the `entry_seq` to resume `after` (null = last page).
  Expand one level per click; **lazy children fall straight out of day-27's `ix_frames_parent`** — no
  new backend, which is the reward for that day's interval encoding.

Render the current path (the stack from `/calltree?seq=currentSeq`) highlighted and auto-expanded
inside the tree; expand the rest lazily as the user opens nodes.

### Edge cases (all are the frontend's to render; the backend already answers them)

- **A 1M-node tree** — virtualise the DOM (render only visible rows) and lazy-load children a page at
  a time. The pagination cap is 1000 nodes/response; the default page is 100.
- **1000-deep recursion** — `parent_frame_id` chains arbitrarily deep; the backend imposes no limit, so
  **cap the visual indentation** and indicate the clip, or it runs off the screen.
- **A frame that never returned** — `exit_seq: null`, `exit_kind: "open"` (a truncated recording, or a
  frame still live at the end). Render "did not return", not a zero-length call.
- **A suspended generator — *live* vs *executing*.** Stack mode returns every frame **live** at `seq`,
  which under generators/async includes frames that are suspended and *not executing*. The executing
  path is the ancestry of `/state`'s `current_frame_id`; a live frame that is **not** on that path is
  live-but-not-executing (a suspended generator) — render it distinctly (dimmed). `/state`'s per-frame
  `suspended` flag is the same signal for the frame the playhead sits in. This live-vs-executing
  distinction (day 27) is exactly what becomes *visible* in this panel.
- **Async: many concurrent "current" frames** — several frames can be live at once. *The* current
  stack is defined as the ancestry of `current_frame_id`; say so in the UI, and show the other live
  frames as concurrent (not as your stack).

## Query panel (day 38) — the backend contract

This is where ChronoTrace's novel contribution — retroactive breakpoints, exception origins,
provenance, watchpoints — becomes a thing a stranger can *use* rather than read the docs to find.

### Forms come from the registry — never hardcode a query

`GET /api/queries` → `QueryDescriptor[]`, each `{ name, summary, args: ArgSpec[] }` with
`ArgSpec = { name, type, required }`. `type` is a wire type name (`"string"`, `"integer"`); build the
input from it, mark `required` fields, and label with `name`/`summary`. **The API describes its own
queries**, introspected from each query's constructor, so a query added on the backend appears in the
panel with its form and **zero frontend change**. Hardcoding the query list here is the one mistake
that makes every future query need frontend work — don't. Today's set: `var-writes`, `line-hits`,
`last-write`, `break` (retroactive breakpoint, with the optional `condition` = the `--if` box),
`exception-origin`, `provenance`, `watch`, `callers-of`, `call-tree`.

### Running a query, and making results feel like time travel

`POST /api/sessions/{id}/query` with `{ name, args, cursor?, limit? }` → `QueryResult
{ hits, next_cursor, partial }`. Each `hit` is `{ seq, file?, lineno?, function?, value_preview?,
note? }` — **`seq` is the answer**, the rest is just enough to choose *which* instant before you
commit.

- **Hover previews, click jumps.** On hover, `GET /state?seq=<hit.seq>` to peek the instant (it is
  preview-only, threadpooled and cancels on disconnect, so peeking is cheap and does not move the
  playhead). On click, set `currentSeq = hit.seq`. Scanning 40 hits by hovering is how a user finds
  "the one" without losing their place — and losing your place is what makes debugging exhausting.
- **Paginate, never render 10M.** `next_cursor` → pass it back as `cursor` for the next page. A hot
  loop's line has millions of hits; the page is the bound.
- **`partial: true`** means the recording is crash-truncated and the answer covers only what survived
  — show it; silently under-reporting is the one thing a debugger must not do.
- **Cancel a slow query** by abandoning the request (the client aborts the fetch); each page is
  bounded work, so there is no unbounded scan to stop.

### Zero results are two different facts — honour the distinction

- **200 with empty `hits`** = the query ran and found nothing (e.g. *no writes* to a real variable).
- **404 `not_found`** = the name/file/function was **never recorded** (a typo, not an empty result) —
  day-28 keeps "there is no `total`" distinct from "`total` never changed". Show different messages.
- **400 `unknown_query`** = no query registered under that name.

### Errors that teach

A bad condition on the `break` query comes back **400 `bad_request`** (bad input — *not* a misleading
404), and its `detail` is written to teach:

- A **syntax error** (`i >`) → the parser's message with a **column** for a caret at the offending
  character.
- A **call** (`foo()`) → "function calls are not allowed in a condition …" plus **why**: a condition
  is a pure test over recorded values, never code that runs (day-30's security rule). An error that
  explains the rule is documentation delivered exactly when it is needed — render `detail` under the
  condition box.

### Right-click a variable → these same queries (no new backend)

From the variables panel: `{name:"var-writes", args:{name}}`, `{name:"provenance", args:{name, seq}}`,
`{name:"watch", args:{name}}`; from the source gutter: `{name:"break", args:{file, lineno}}` (+
`condition`). All return `hits[].seq` jumps, rendered by the same results list.

## Production build

```bash
cd frontend && npm run build          # emits into ../src/chronotrace/_ui
pip wheel . --no-deps -w dist         # the wheel now carries the UI (day 44 automates this)
chronotrace serve --dir ./recordings  # one process serves API + UI at http://127.0.0.1:8000
```
