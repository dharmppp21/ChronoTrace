# Spikes

**Throwaway code. Do not import this from `chronotrace`.**

A spike answers one risky question with a measurement, then dies. It is not a
prototype and it is not the first draft of a feature — it exists so that an
architectural bet is made on evidence instead of optimism.

## Rules

- Spike code is **excluded from the package** (see `pyproject.toml`) and runs
  under a lighter lint/type standard. The *methodology* must be rigorous; the
  code need not be beautiful.
- Nothing in `src/chronotrace/` may import from here. CI enforces the package
  contents; the import graph test (day 10) enforces the direction.
- The deliverable is the `RESULTS-*.md` file, not the script. If the script
  vanished tomorrow and the results survived, we lost nothing important.
- When a spike's finding is promoted into real code, **re-derive it from the
  findings** rather than copying the spike. The spike's job was to teach us the
  answer, not to hand us an implementation.

## Current spikes

| File | Question | Answer |
|---|---|---|
| `bench_overhead.py` | Can we observe every line of a Python program at a cost a developer will tolerate? | [RESULTS-overhead.md](RESULTS-overhead.md) |
| `workloads.py` | (support) Four workloads that represent real programs, not microbenchmark lies | — |

`workloads.py` is the one file here with a second life: day 43 reuses these same
workloads for the published benchmark suite, so they are kept honest and
dependency-free.
