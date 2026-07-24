# The replay-equivalence harness

**The referee.** Every other test in ChronoTrace checks ChronoTrace against ChronoTrace.
This one checks it against reality.

## What it proves

> The state ChronoTrace reconstructs at instant `seq` is the state the program **actually
> had** at that instant, as observed live, by a mechanism that shares no code with the
> recorder.

That claim spans every subsystem at once — recorder, capture, dedup, value pool, writer,
keyframes, deltas, reader, reconstructor. It is the only test in the project that can
catch several of them being wrong *together*.

Day 20's oracle proves the fast reconstruction equals the slow one. That is a real and
useful property, and it is entirely internal: if the **recorder** misunderstood the
program, the fast path and the slow path are confidently wrong together and every test
stays green. Only an independent observer notices.

## Why the truth source must be independent

A truth source built from the recorder's own machinery inherits the recorder's mistakes.

- If `FrameRegistry` fuses two frames, a truth source that asks `FrameRegistry` which
  frame is current agrees with the fusion.
- If the seq counter double-counts, a truth source reading that counter agrees.
- If change detection wrongly decides a local is unchanged, a truth source built on
  change detection never looks at the local either.

The test would then assert `X == X`, pass forever, and ship a debugger that lies. **The
independence is the entire value of the harness**, not a stylistic preference.

So `truth.py` is a second `sys.monitoring` tool under its own tool id, with its own
callback, reading `frame.f_locals` directly. It never imports `FrameRegistry`, the seq
counter, `Event`, `ValuePool`, the dedup cache, or the recorder's capture-of-locals path.

### What it *does* share, and the cost of that

Three pure predicates: **`capture`** (how an object becomes bounded plain data),
**`Redactor`** (which names are secrets), **`Scope`** (which files count).

`capture` is shared because comparing a bounded representation against a raw object is
not a comparison — every long list would read as a mismatch. The honest cost, stated
plainly: **a bug inside `capture` itself is invisible to this harness.** That is covered
by `tests/recorder/test_capture.py`, which checks the capture zoo against hand-written
expectations rather than against itself. `Redactor` and `Scope` are name and filename
predicates with their own tests; reimplementing them here would test our ability to write
globs twice.

Everything that can actually be wrong — which frame is live, which instant is which, what
changed, what was deduplicated, encoded, or reconstructed — is observed independently.

## What lossiness is legitimate

Exactly one thing: **object-identity ids**. `capture` stamps a durable `id` into tagged
dicts when given an `ObjectIdentity`. The recorder has one; the observer deliberately does
not, because sharing the recorder's would perturb its id assignment. Two independent
identity maps cannot agree by construction, so ids are stripped from both sides before
comparing. Aliasing is checked in `test_capture.py`.

**Nothing else is forgiven.** Truncation, depth limits and redaction are not allowances:
the observer applies the same policy and the same redactor, so a truncated list must match
a *truncated* list exactly, and a secret must be `REDACTED` on both sides. A comparator
that forgave truncation would forgive a value being wrong past element 100.

Two things are compared structurally rather than by value, and both are stated in
`compare.py`:

- **Outer frames' binding values.** A caller's locals are only as fresh as the last line
  that caller executed, because that is the only time the recorder rescans them. Comparing
  them against `f_locals` *now* would fail the system for a claim it never made. The
  staleness is real and tracked as [#8](https://github.com/dharmppp21/ChronoTrace/issues/8)
  — not smuggled in here as a tolerance.
- **The stack**, which is checked as the day-6 model: every in-scope frame on the real
  `f_back` chain must be live, at the right line, in the right order, and any *extra* live
  frame must be `suspended`. That is the registry claim ("live frames are the stack plus
  suspended generators") written as an assertion.

## Proven to fail

A test suite that has never been proven to fail is not evidence of anything. Four bugs are
deliberately injected, and the harness must go red for each:

| injected bug | layer | why it is the interesting one |
|---|---|---|
| a dropped delta | reconstruct | state stays plausible and is wrong |
| a keyframe that under-reports state | store | what a writer bug actually looks like downstream |
| content-blind dedup | recorder | the day-8 bug: a mutable mutated under a stable `id()` |
| a drifting reconstruction cache | reconstruct | ADR-0006 §4's named nightmare — nothing about the answer looks wrong |

A fifth case asserts the opposite: **dropping half the keyframes must leave the harness
green**, because day 15 designed keyframes so a lost one costs latency, never correctness.
That turns graceful degradation from a story into a property.

## How to read a failure

```
  seq 104  [extra]  generators.py:60 in abandoned_generator
    gen
      real:          'not a local here'
      reconstructed: {'$': 'obj', 'type': 'generator', 'module': 'builtins', 'opaque': True}
    reconstructed from keyframe 64 (40 events back), replaying 15 deltas
```

- **`seq`** — the instant. Feed it to `chronotrace step` or `reconstruct(seq)`.
- **`[kind]`** — `value` (both present, different), `missing` (real but not reconstructed),
  `extra` (reconstructed but not real), `frame` (wrong position), `stack` (wrong shape).
- **the last line** — how reconstruction got there. A failure that only happens far from a
  keyframe points at delta replay; one that happens *at* a keyframe points at the keyframe.

Then shrink it:

```python
from tests.equivalence.minimise import harness_oracle, minimise

print(minimise(source, harness_oracle(tmp_path, mismatch)))
```

Minimisation reduced the `del` failure below from 14 lines to 6 in 0.2 s. The oracle
matches on the mismatch *kind and variable*, not merely "something went wrong", so it
cannot shrink towards a different bug.

## Known divergences

| program | divergence | tracked |
|---|---|---|
| `generators.py` | `del x` leaves the binding alive in reconstruction forever | [#7](https://github.com/dharmppp21/ChronoTrace/issues/7) |

Found by this harness on its first run. Listed as an **exact** expectation in
`test_equivalence.py`, not forgiven in the comparator: when the recorder learns about
deletion, that assertion fails and the entry gets deleted. That is the point.

## Adding a program

Put it in `tests/fixtures/` (not in this package — the observer must never observe
itself) and add it to `PROGRAMS`. Every program in the repo is a correctness case.
