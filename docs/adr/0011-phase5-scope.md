# ADR-0011: Phase 5 ships a scrubber, not an IDE

**Status:** accepted · **Date:** day 32 · **Context:** the roadmap's risk register names one
failure mode for Phase 5 by name — *"scope creep into a full IDE."* A UI is the most tempting
place in the whole project to keep adding "just one more panel." This ADR writes the boundary
down **before** the UI exists, because a scope decision made under the temptation is not a
decision, it is a rationalisation.

## The one-sentence scope

**Phase 5 is a read-only viewer for one recording: scrub time, see state, run queries.** Five
panels — timeline scrubber, source + heatmap, variables, call tree, query — over the day-32 API
and nothing else. It makes the engine *seeable*. It does not make it an editor, a workbench, or
a platform.

## Non-goals — the actual output of this ADR

Each of these is a real thing someone will ask for, and each is **explicitly out of Phase 5**.
The value of the list is that "no" is already written down, with a "when".

- **No code editing.** The source pane is read-only. ChronoTrace shows what *ran*; editing is
  the editor's job, and a recording of edited code is a recording of a different program.
  *(Never in scope — it is a category error, not a deferral.)*
- **No breakpoint UI in the source gutter** beyond surfacing the retroactive-breakpoint *query*.
  Clicking a line may run `--break file:line`; it does not install a live breakpoint, because
  there is nothing live to break. *(Post-1.0 if the query framing proves too indirect.)*
- **No multi-session diff.** One recording at a time. Comparing two runs ("what changed between
  the passing and failing run") is a genuinely good feature and a genuinely large one — its own
  phase, not a panel bolted on here. *(Post-1.0.)*
- **No VS Code / JetBrains extension.** The browser UI is the product for Phase 5. An editor
  extension is a second frontend with its own protocol surface and review burden. *(Post-1.0;
  the day-32 HTTP API is what an extension would eventually consume, which is why the API is
  designed to outlive this UI.)*
- **No account, no persistence, no sharing.** Localhost, one user, no server-side state beyond
  the recording files on disk (ADR-0010, threat model). Sharing a session is "send the
  `.chrono`." *(Post-1.0, and only with the day-47 security review done first.)*
- **No write operations at all.** The API is read-only except `POST /query` (which computes,
  never mutates) and the day-34 live stream (which observes). Nothing the UI does changes a
  recording. *(Never — immutability is a load-bearing correctness property, not a limitation.)*
- **No plugin system, no theming API, no configurability beyond what a query needs.** The
  temptation to build the extension point before the extension. *(YAGNI; add the seam when a
  second real caller exists, per the project's standing rule.)*

## Why write this today

Two reasons, both learned earlier in this project:

1. **The dependency arrow only holds if the scope does.** The whole reason `server` may not
   leak a storage type (ADR-0010) is to keep the layers free to change. An IDE's worth of
   features would pull requirements *up* the stack — "the diff panel needs the format to store
   X" — and the arrow would bend. A small viewer keeps the pressure off.
2. **The demo is a scrubber.** The thing that stops a recruiter scrolling is *scrub backward
   through a real bug and watch the state change* — one clear motion, thirty seconds. Every
   panel that is not in service of that motion is weight the demo carries and does not spend.

## Decision

Build the five panels, read-only, over the day-32 API. When a feature request arrives, check it
against the non-goals above: if it is listed, the answer is the written "when," not a new
panel. This ADR is the thing to point at in week 8.

**Reversal trigger:** none for the "never" items (they are category decisions). For the
"post-1.0" items, the trigger is 1.0 shipping with the scrubber demonstrably solid — earn the
second frontend after the first one is proven, not before.

## Checkpoint-5 reconciliation (day 39) — did it stay a scrubber?

Honest verdict, backend side: **yes.** Every Phase-5 endpoint (days 33–38) is a *read* or a
*compute*, and there is no write path. Walking the non-goals against what shipped:

- **No write operations.** The full surface is `GET` reads (`sessions`, `{id}`, `timeline`,
  `state`, `source`, `calltree`, `calltree/children`, `value`, `step`, `diff`, `queries`),
  `POST /query` (computes a page of instants, mutates nothing), and `WS /stream` (observes a
  growing file). The recording is opened read-only. Immutability held. ✓
- **The retroactive breakpoint stayed a *query*, not a gutter breakpoint.** Clicking a line runs
  `POST /query {name:"break"}`; no endpoint installs a live breakpoint, because there is nothing
  live to break — exactly non-goal #2. ✓
- **`/diff` is *within-session*, not multi-session.** Day 37's `GET /diff?seq=` reports one
  instant's variable changes from the day-16 deltas. It is not the "compare two runs" feature
  that non-goal #3 defers — the name is the only thing that could be misread, and it is worth
  saying so here. ✓
- **`/api/queries` describes queries; it is not a config/plugin API** (non-goal #7). It enumerates
  what the engine already offers so a form can be built from it — no extension point, no
  configurability beyond a query's own arguments. ✓
- No account/persistence/sharing, no code editing, no editor extension — none shipped. ✓

Two honest notes, neither a drift:
- The endpoint table in **ADR-0010 predated `/diff`, `/calltree/children` and `/api/queries`**;
  those were added within scope and the table is reconciled in the same checkpoint. The
  authoritative contract is `/openapi.json` (ADR-0010 decision 7), which cannot drift.
- The "clicking a line does not install a live breakpoint" guarantee is the **frontend's** to keep
  as well; it is called out in `docs/frontend-integration.md` and is a frontend-review checklist
  item, not something the backend can enforce (the backend simply offers no such endpoint).

The pressure ADR-0011 was written to resist — "just one more panel" pulling requirements up the
stack — did not materialise on the backend: the five panels shipped over the day-32 API and the
dependency arrow (`test_no_layer_imports_a_higher_layer`, `test_no_storage_type_can_reach_the_wire`)
is still green. **Scope held.**
