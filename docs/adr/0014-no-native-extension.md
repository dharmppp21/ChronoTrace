# ADR-0014: No native (Rust/PyO3) extension — declined on the numbers

**Status:** accepted · **Date:** day 42 · **Context:** the roadmap made a native capture extension
*conditional on evidence* from the start — the Phase-6 rule is "skipping it with data is a win, not
a failure." Day 40 profiled the recorder and computed the Amdahl ceiling; day 41 optimised the top
Python bottlenecks and moved the landscape; day 42 re-profiled and decides. This ADR is that
decision, with the arithmetic, and — the part that makes it engineering rather than dogma — the
trigger that would reverse it.

## The measurement (recorder self-time, value capture ON, realistic `json_pipeline`, py-spy)

| region | day 40 | **day 42 (post-opt)** | native-replaceable? |
|---|---:|---:|---|
| value capture traversal | ~55% | **53.3%** | yes — a native walk |
| content digest (`repr` + blake2b) | ~25% | **27.4%** | yes — hash during the walk |
| `ObjectIdentity.of` | ~7-13% | **7.7%** | partly — CPython weakrefs stay |
| allocation (`__new__`) | ~6% | **6.2%** | partly — the result is Python objects |
| redaction + scope | ~2% | **0.7%** | no |

Note what day 41 did to this table: optimising the *cheap* Python parts (the redaction regex, the
atom fast path) made the profile **more** capture-dominated, not less — redaction fell to 0.7%. So
the replaceable share went *up*, and the honest reading is that the case for Rust got **stronger**,
not weaker. This ADR declines it anyway, and the reasons why are the point.

## The Amdahl calculation

Cleanly replaceable core (capture walk + digest) = **~80%**. Identity and allocation (~14%) are
only *partly* replaceable — a native capturer must still build the result as Python objects for the
value pool and serialisation, and interact with CPython weakrefs — so count them at roughly half:
**effective replaceable ≈ 87%**. The rest (~13%) is CPython's `sys.monitoring` dispatch and the
Python glue in `_on_line`, which a native extension cannot delete because the callback *is* Python.

- **Infinite Rust:** `1 / (1 − 0.87)` = **~7.7×** ceiling.
- **Realistic (6× on the replaceable part):** `1 / (0.13 + 0.87/6)` = **~3.6×**.

At today's ~2000× value-capture overhead, a 3.6× win lands at ~550× — still slow, because value
capture is inherently expensive. And it moves *nothing* for flow-only recording, which is already
5.4× (RESULTS.md), comfortably under ADR-0001's 20× budget.

## The decision: DECLINE — and why, despite a ~7.7× ceiling

The naive read is "the ceiling is high, so build it." That is wrong here, for four reasons the
Amdahl number alone does not capture:

1. **The replaceable region is not a narrow hot function — it is the entire capture subsystem.**
   `capture.py` is ~380 lines of the most correctness-critical, hostile-input-hardened code in the
   project: four interacting bounds (depth/items/str-len/node-budget), cycle detection, the
   never-invoke-user-code invariant (reads `__dict__`, never `getattr`), never-retain, identity
   assignment, the tagged representation. Rewriting *that* in Rust — correctly, differential-tested
   against the whole day-7 hostile zoo — is not a clever afternoon on one function; it is the
   riskiest week in the project, and a permanent second implementation of the scariest code.
2. **No user is asking.** There are no users yet; this is a portfolio project pre-1.0. Optimising a
   cost nobody has hit is speculative work by definition, and the overhead is *opt-in*: value
   capture is a flag, and flow-only recording is already fast.
3. **Sampling beats it on the actual problem, at a fraction of the cost.** The pain is hot loops.
   `--sample` (record every Nth hit of a line) changes the **asymptotics** — O(lines) → O(lines/N) —
   which for a tight loop dwarfs any *constant*-factor 3.6× native win, and costs days (plus the
   META-honesty plumbing below), not a week plus a permanent build matrix. Changing the asymptotics
   is the better lever; the native rewrite only shrinks the constant.
4. **The week is better spent.** Days 44–50 are packaging, security, docs, and the demo video. For
   the project's actual goal — a portfolio piece — **a better demo video is worth more than a 30%
   overhead win no user requested.** The native extension would also cost a build matrix
   (Linux/macOS/Windows × 3 Pythons), cibuildwheel, an sdist fallback for compiler-less machines, a
   runtime fallback to pure Python on import failure, `catch_unwind` at every FFI boundary (a
   debugger that segfaults the program it is debugging is beyond useless), and Rust in the
   contribution path — **two maintenance surfaces, forever.**

Declining a rewrite whose Amdahl ceiling is genuinely high, on the strength of risk, demand, a
better alternative, and opportunity cost, is a more honest engineering call than shipping it because
the ceiling looked good. That is the decision.

## Reversal trigger

Revisit the native extension if **any** of these becomes true, measured, not assumed:

- A user reports recorder overhead as **blocking** on a realistic workload they actually run, and
  `--sample` does not resolve it.
- After further Python-level work **and** shipping `--sample`, value capture still exceeds ~50% of a
  realistic profile *and* the absolute overhead is a stated adoption blocker.
- Free-threaded Python (PEP 703) makes a parallel native capturer a qualitatively different win (it
  could capture off the traced thread) — a new argument the current GIL-bound analysis does not cover.

Absent a trigger, the answer stays no, and the arithmetic above is why.

## The real overhead answer, tracked (not built today)

`--sample` is the right overhead lever, but it is a genuine multi-day feature, not a hack: a lossy
recording **must** be marked honestly (Day-7's truncation rule), and the `.chrono` **META block is
still empty** (`writer.py`, deferred since day 14). So `--sample` requires, in order: (1) build the
META block — write the config/sample-rate, read it at open, expose it via `SessionMeta`; (2) the
recorder-side sampling (a per-`(code,line)` hit counter in `_on_line`, always recording the first
hit so no line is lost, sampling the rest); (3) the UI surfacing the "sampled" state, exactly like
truncation. Tracked as a GitHub issue with this design; it is the next overhead step, ahead of any
native work.

## Consequences

Zero code shipped for the extension — deliberately. The recorder stays pure Python, zero-runtime-dep,
one implementation. The overhead story becomes honest and contextualised in the README (comparable
to `pdb`'s tracing overhead; value capture opt-in; `--sample` planned). **Reversal trigger above; the
data is in `benchmarks/PROFILE.md` and this ADR.**
