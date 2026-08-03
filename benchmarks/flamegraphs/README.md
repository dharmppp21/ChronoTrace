# Recorder flamegraphs — before and after the day-41 optimisations

py-spy sampling profiles of the recorder over the four workloads, at 300 Hz. Sampling
(not `cProfile`) because instrumentation would distort the per-line callbacks that
*are* the hot path — the reasoning is in [`../PROFILE.md`](../PROFILE.md).

- **`before/`** — day 40, before any optimisation (the baseline profile that
  [ADR-0013](../../docs/adr/0013-performance-plan.md) triaged).
- **`after/`** — day 43, after the day-41 wins landed
  ([`../OPTIMIZATIONS.md`](../OPTIMIZATIONS.md)).

Regenerate the after set with:

```bash
python benchmarks/profile_recorder.py flamegraphs --subdir after
```

## What changed between them

Day 41 optimised the two *cheap* Python parts of the profile, and the measured shift
is quantified in [ADR-0014's table](../../docs/adr/0014-no-native-extension.md):

| region | before (day 40) | after (day 42) |
|---|---:|---:|
| value capture traversal | ~55% | 53.3% |
| content digest (`repr` + blake2b) | ~25% | 27.4% |
| redaction + scope | ~2% | **0.7%** |

The redaction bar all but disappears (a compiled-regex redactor, −78% per name), and
the capture bar narrows slightly (a leaf-atom fast path, −11–16% on common values).
The instructive part is what the "after" profile then looks like: optimising the
cheap parts made the profile **more** capture-dominated, which is precisely why a
native rewrite's Amdahl ceiling *rose* — and why ADR-0014 still declined it. The
flamegraphs are the picture of that argument.
