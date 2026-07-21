# The property campaign

Day 22 built a referee that checks reconstruction against reality. It can only check the
programs you point it at, and until today those were five examples a human thought to
write. **Humans write the code they already have in mind; the bugs live in the code they
do not.** This generates programs nobody would think to write and hands each one to the
referee.

## The properties, in plain English

| property | in code | what it protects |
|---|---|---|
| For any generated program, at every sampled instant, reconstructed state equals the state the program actually had | `test_pipeline.py` | everything from days 4–22, at once |
| Undoing a delta restores the state exactly | `invert(apply(s, d)) == s` | day 16 — invertibility, the reason `old_ref` is stored |
| What the writer wrote is what the reader reads | `read(write(events)) == events` | days 12–13 — the columnar codec, across block boundaries |
| Stepping backward undoes stepping forward | `step_back(step_forward(seq)) == seq` | day 21 — forward and backward cannot drift |
| Reaching any instant replays at most `interval` events | `seq - keyframe.seq <= interval` | ADR-0006's cost proof, asserted not assumed |

The first is the one that matters. The other four exist so that when it goes red you know
*which layer* — the difference between a bisect and a guess.

## What the generator covers

Nested functions · loops · conditionals · bounded recursion · generators · try/except/
finally · closures and `nonlocal` (including two levels up) · comprehensions and their
implicit scope · `del` and rebinding · shadowing a global · `*args`/`**kwargs` · mutable
default arguments accumulating across calls · classes · `raise` inside `finally` ·
`return` inside `finally` · a generator abandoned before exhaustion.

Coverage is **measured, not assumed**: `test_the_generator_reaches_every_required_construct`
fails if any of those never appears in 150 draws. "The generator probably covers that" is
exactly how a campaign ends up proving nothing.

### What it deliberately does not cover

- **Threads** — the recorder is single-threaded and `ProgramState` has no thread
  dimension. Generating them would test a claim the system does not make.
- **`eval`/`exec`** — code objects belonging to no file, which the scope filter cannot
  attribute and the intern table would grow without bound.
- **C extensions** — no Python frames, so nothing to record.
- **Imports** — the stdlib is out of scope by design, so an import adds no recordable
  frames, only nondeterministic module-level side effects.
- **`async`** — already covered by `examples/generators.py` in the referee; generating
  well-formed coroutines needs an event-loop harness around every example.
- **`while`** — cannot be expressed. See "terminating by construction" below.

## Three design decisions

**A grammar over structure, not over text.** Generating characters and hoping they parse
wastes essentially every draw, and — worse — Hypothesis would shrink towards shorter
*strings* rather than simpler *programs*, so a failure would minimise to unreadable
rubble. The tree is two node types (`Line`, `Block`), which is exactly the
indentation-sensitive part of Python and the only thing a text generator gets wrong.

**Terminating and deterministic by construction, never by timeout.** A generated program
that does not stop hangs CI with no failing example to look at; one that depends on hash
ordering or the clock produces failures that vanish on rerun, which costs days. So loops
are only `for _ in range(k)` with a literal bound, recursion always carries a guarded
countdown, and there is no I/O, clock, `id()`, randomness or `set`. A timeout tells you
the program hung; a grammar that cannot express non-termination means it never does.

**Context-sensitive generation.** An `Env` threads through every production carrying which
names are bound, which are `nonlocal`-able, what type each holds, and which may be
deleted. A context-free grammar cannot express "`nonlocal x` requires x bound in an
enclosing function scope", and without that the interesting constructs would only appear
by luck.

## What the campaign found

Property testing found **seven real bugs on day one**. All were in the campaign rather
than in ChronoTrace — which is itself the finding, since a campaign that cannot generate
valid programs, or cannot reach the storage layer, proves nothing about the code:

1. **`global` after a local binding** — a `SyntaxError`; the production was dropped in
   favour of the shadowing case that was actually required.
2. **`del` inside a nested block** — a nested `if`/`try` could delete a name the enclosing
   block still believed was bound, so any later reference was an `UnboundLocalError`.
   Bindings and deletions are not symmetric: a binding inside an `if` does not escape it,
   a deletion does. Fixed with `Env.nested()`.
3. **A deleted parameter in the function tail** — `return p1` after the body deleted `p1`.
   The tail now reads the environment as it actually is.
4. **`next()` on a generator that returns before yielding** — `StopIteration`, an
   accidental exception indistinguishable from a recorder crash. Now `next(g, None)`.
5. **The campaign reached almost none of the storage layer.** The first 400 programs ran
   clean, which looked like good news. Measuring showed a median of **20 events and 0.3
   keyframes per program** — almost every recording had one keyframe, at seq 0, so
   reconstruction never crossed a keyframe boundary. Drawing `keyframe_interval` and
   `block_events` from small ranges fixed it: a 20-event recording at interval 2 crosses
   ten keyframe boundaries. **This is why the day's checklist says to suspect the
   generator when it finds nothing.**
6. **A list deleted and rebound as an int** — `v0 = []; del v0; v0 = 8; len(v0)`. The
   rebind always produced an integer while the environment still filed the name under
   lists, so a later `len()` was a `TypeError` the grammar never meant to generate. Found
   only by the *deep* campaign: it needs a delete, a rebind and a later `len` in one
   program, which the 40-example profile never assembled. The fast profile would have
   shipped this.
7. **`nonlocal` was in the grammar and effectively untested** — 8 programs in 400,
   because closures need a function nested inside a function that has already bound
   something. Weighting the production and seeding `main` with one local raised it, and
   the coverage sample is now sized for it rather than for convenience.

The pipeline itself survived the campaign. That is meaningful only because
`test_the_campaign_catches_an_injected_bug` proves the campaign can fail: it drops one
delta and asserts the generated programs notice.

## Reproducing a failure

Hypothesis writes every failing example to `.hypothesis/examples`, and replays it first on
the next run — so a red campaign reproduces locally with a plain `pytest tests/property`.
The nightly workflow uploads that database as an artifact for the same reason.

The failure message is the generated program followed by the referee's mismatches (see
[`tests/equivalence/README.md`](../equivalence/README.md) for how to read one). To shrink
it further than Hypothesis did, hand it to day 22's minimiser:

```python
from tests.equivalence.minimise import harness_oracle, minimise
print(minimise(source, harness_oracle(tmp_path, mismatch)))
```

Module names are hashed from the source **deliberately**: Hypothesis re-runs a failing
example to confirm it and again while shrinking, and if each run wrote a differently-named
module the traceback would differ and Hypothesis would declare the failure flaky and give
up. That happened before the name became a content hash. Determinism in the harness is a
precondition for shrinking, not a nicety.

## Profiles

| profile | examples | where |
|---|---:|---|
| `ci` | 40 | every push, all nine matrix cells |
| `dev` | 100 | local default |
| `nightly` | 3,000 | `.github/workflows/nightly.yml`, 03:00 daily, py3.12–3.14 |

Depth and feedback speed are on different clocks on purpose: bugs are found in proportion
to examples tried, but a per-push check that takes twenty minutes to report a failure is
one people learn to ignore.

```bash
pytest tests/property --hypothesis-profile=nightly
```
