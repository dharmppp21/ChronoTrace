# The ChronoTrace UI

The browser UI is a **read-only viewer for one recording**: scrub time, see state, run queries
(ADR-0011). It is a *scrubber, not an IDE* — it makes the engine seeable, it does not edit code or
mutate a recording. Launch it with:

```bash
pip install -e ".[ui]"
chronotrace record examples/buggy_pipeline.py --ui   # record, then scrub
# or, over existing recordings:
chronotrace serve --dir ./recordings
```

## The one idea: the UI is a pure function of `currentSeq`

Every panel renders the same instant. There is exactly one piece of shared state — the current
event index (`currentSeq`) — and the timeline, source, variables, call tree and query results are
all *derived* from it. Move the playhead and everything re-renders together; nothing holds its own
copy of "where we are." This is why scrubbing is coherent and why a query result can move the whole
UI by setting one number. (`seq` is an internal address — the UI shows it as a step/position, never
asks the user to think in event indices.)

Each panel talks to the server **only** over the generated OpenAPI client (never a hand-written
`fetch`); the wire contract is [ADR-0010](adr/0010-api-contract.md) and the panel-by-panel backend
seam is [`frontend-integration.md`](frontend-integration.md).

## The five panels

### 1. Timeline scrubber
**What it shows:** the whole recording as a density band — where execution was busy — with the
playhead at `currentSeq` and the truncation boundary drawn if the recording was crash-cut.
**Backend:** `GET /timeline?buckets=N` (day-27 density). **Interaction:** drag to travel; the drag
is rAF-coalesced on the client and each superseded `/state` request is cancelled on the server, so
dragging fast does no wasted reconstruction. This is *the* motion of the product.

### 2. Source + heatmap
**What it shows:** the file for the current frame, with the executing line highlighted and a
per-line execution heatmap (log-scaled — a linear scale renders everything but the hottest loop as
identically cold). **Backend:** `GET /source?file=` (`available:false` → show the file, withhold the
overlay + a "source changed" banner). **Interaction:** click the gutter → a *retroactive breakpoint*
(`POST /query {name:"break"}`) — every instant that line ran, as jumps. The never-taken branch is
the most valuable pixel: a real line absent from the heatmap ran zero times.

### 3. Variables
**What it shows:** the locals of the current frame as `name → preview`, lazily expandable
(`GET /value?ref=`, one level per click — a 10k-element list ships one preview, not 10k rows). As
you scrub **backward, variables change backward.** **Backend:** `GET /state?seq=` for the locals,
`GET /diff?seq=` for the change badges (added / removed / modified, `old → new`) computed server-side
from the day-16 invertible deltas. Lossy-capture states are honest, hoverable badges
(`<redacted>`, `<budget>`, `<depth>`, `<cycle>`, `…`), never a blank. Right-click a variable → its
write history / provenance / watch (all `POST /query`).

### 4. Call tree
**What it shows:** not just the current call *stack* but the whole call *tree* — every call the
program ever made — with the current path highlighted, and each node coloured by how it ended:
returned, **raised** (unwound by an exception — the one-glance "where did it blow up?"), or open
(never returned). **Backend:** `GET /calltree?seq=` (stack, live frames) and
`GET /calltree/children?parent=` (lazy tree expansion; omit `parent` for the forest roots).
**Interaction:** click a node → jump to its call; also jump-to-return (`exit_seq`) and jump-to-caller
(`parent_frame_id`). You can click a call that returned 200k events ago and *go there* — no live
debugger can. A suspended generator is rendered distinctly (live but not executing).

### 5. Query
**What it shows:** the engine's queries, each as a form **generated from the API** (`GET /api/queries`
— add a query on the backend and it appears here with no frontend change), and results as
jump-to-instant links. **Backend:** `POST /query`. **Interaction:** hover a result to *preview* the
instant without committing (peek `GET /state?seq=`); click to jump. A malformed `--if` condition
comes back as a teaching error (the parser's column for a syntax error; the rule for a forbidden
call), rendered under the condition box. Zero results say *why*: no writes (empty page) vs. no such
variable (404) are different facts.

## Keyboard shortcuts

The canonical bindings mirror the `chronotrace step` REPL, so the two surfaces agree (the frontend
implements these; they are the contract, not a suggestion):

| key | action | key | action |
|---|---|---|---|
| `←` / `→` | scrub one step back / forward | `n` / `p` | next / previous line (step into, either way) |
| `Home` / `End` | first / last instant | `o` / `O` | step over, forward / backward (skip nested calls) |
| `Space` | play / pause auto-scrub | `f` / `F` | run to this frame's exit / back to its call |
| `/` | focus the query box | `?` | keyboard-shortcut overlay |

Backward and forward are the *same* engine operation with the scan sign flipped
(`step_back(step_forward(seq)) == seq`), so the two directions cannot disagree.

## States the UI must always show (never a blank panel)

- **Empty** — no recording open: tell the stranger what to do first ("drag the timeline to travel
  through your program's execution"), not a blank canvas.
- **Loading** — a skeleton while `/state` resolves, never a flash of nothing.
- **Error** — a `Problem` (`{code, detail}`) rendered as a human sentence + the action, never a raw
  stack trace. The error codes and their user actions are ADR-0010's error table.

## What the UI deliberately is not

Read-only, one recording, localhost, no accounts, no code editing, no live breakpoints (the
retroactive-breakpoint *query* is the framing). The full non-goals list, with a "when" for each, is
[ADR-0011](adr/0011-phase5-scope.md).
