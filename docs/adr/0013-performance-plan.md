# ADR-0013: The performance plan, decided by the day-40 profile

**Status:** accepted · **Date:** day 40 · **Context:** Phase 6's whole risk is optimising the
wrong thing — the roadmap warns by name against a speculative Rust extension and against
polishing a cold path. Day 40 profiled the recorder and the pipeline (py-spy, cProfile,
micro-timers; four workloads; flamegraphs in `benchmarks/flamegraphs/`, findings in
`benchmarks/PROFILE.md`). This ADR turns that data into decisions, so no optimisation lands
without a number behind it.

## The measured picture (recorder, value capture ON, realistic workload)

Value capture is ~99% of the *marginal* cost of turning capture on (flow-only is 5.4×, capture is
~2100×). Within capture, self-time splits: **traversal ~55%, `repr`+blake2b digest ~25%, object
identity ~7%, allocation ~6%, redaction/scope ~2%** — except on cheap-scalar workloads, where the
per-local **redaction `fnmatch` inverts to ~30%**. GC is healthy (no gen-2 thrash, linear memory);
`DISABLE` still fires; the downstream stages (index, reconstruction, query, WS) are cold paths.

## Decision 1 — optimise the recorder hot path only; leave every cold path alone

The recorder runs per line of the traced program; reconstruction/query/WS run per human action. A
1 µs recorder saving is worth millions of times; a 1 ms reconstruction saving happens once behind a
reflex and is invisible. **No cold-path optimisation is planned.** Their current numbers
(RESULTS.md) already meet their budgets, and touching them is the wasted week Phase 6 warns about.

## Decision 2 — bank the cheap Python wins first, in ROI order (Days 41+)

Each is Python-only, no native dependency, and carries a before/after number as its ticket:

1. **Redaction: compile the secret-name globs to one regex** (or cache the decision per `(code,
   line)` — local names are static per line). ~30% of cheap-value overhead, ~an afternoon, low risk.
2. **Structural digest: hash the captured tree during capture**, dropping the separate `repr`
   string + walk. ~25%, medium risk (the dedup invariant must hold).
3. **Lazy object identity: assign ids only when a recording will be opened in the UI** (issue #9's
   aliasing badge is the only consumer). ~7-13%, feature-gated.
4. **Exact-type dict dispatch before the isinstance fallback in `_handler_for`** — day-7 measured
   this at 113 ns vs 202 ns; the profile suggests it regressed. ~5%, low risk.

## Decision 3 — the Rust extension is DEFERRED, by Amdahl's law, not by preference

~88% of value-capture overhead is Python a native pass could replace, so Amdahl caps a Rust rewrite
at ~8× (infinite Rust) and ~3.7× (realistic 6×). But: (a) flow-only already meets the budget, so
only value capture is slow and nothing forces it faster today; (b) the Decision-2 Python wins
deliver much of that multiple at a fraction of the cost; (c) a native extension is a compiled
dependency **inside the traced process**, breaking the recorder's zero-runtime-dep invariant
(ADR-0001), plus wheels for three OSes and a build matrix.

**Reversal trigger:** re-profile after the Decision-2 wins ship. Build the Rust capturer only if
value-capture latency then demonstrably blocks a real user — measured, not assumed. Skipping it
*with data* is the intended Phase-6 outcome.

## Consequences

Days 41+ have a ranked, numbered backlog instead of a hunch. Nothing was optimised on day 40 — the
discipline was producing the data that says what to optimise and, more valuably, what not to. Each
future perf commit must carry its before/after number or it does not land.
