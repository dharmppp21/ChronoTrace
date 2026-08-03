# Benchmark methodology

This document is what makes the numbers credible. A table without its method is a
claim without evidence; an interviewer who cannot see how a number was produced is
right to discount it. So the rules below are stated before the results, not after.

Regenerate everything with `python -m benchmarks` (see [Reproducing](#reproducing)).

## What is measured, and against what

The headline table in [RESULTS.md](RESULTS.md) compares recording overhead against
the tools a Python developer actually reaches for, on four fixed workloads
(`spikes/workloads.py`). Each condition does **strictly more work** than the one
above it — this is the single most important thing to understand about the table:

| Condition | What it does | Work per line |
|---|---|---|
| baseline | nothing | none |
| `sys.settrace` no-op | calls a Python function that returns itself | a call |
| pdb (never-hit breakpoint) | full pdb per-line dispatch, condition eval, never stops | a call + break check |
| coverage.py | records the **set** of lines that executed | set insert |
| chronotrace (flow only) | records the **ordered stream** of every LINE/CALL/RETURN | event append, scoped |
| chronotrace (+capture) | records every local variable's **value** at every line | bounded value walk + hash + dedup |

### The fairness rule

**These tools do different amounts of work, so raw ratios are not a like-for-like
race.** Two consequences a knowledgeable reader will look for, stated up front:

1. **coverage.py stores a line set; ChronoTrace stores an ordered event stream and,
   with capture on, every value.** ChronoTrace records far more. If a ChronoTrace
   row is *faster* than coverage.py, that is **not** a per-event speed win — it is
   almost always **scope** (below) recording fewer lines. Do not read it as "we beat
   coverage." We measure more and sometimes still finish sooner because we measure
   *less code*; those are different claims and the table keeps them separate.
2. **`sys.monitoring` is not a cheaper per-event path than `settrace`.** On a
   fully in-scope tight loop the two mechanisms cost roughly the same
   (`spikes/RESULTS-overhead.md`, Finding 1). PEP 669's advantage is *structural*
   (it can be told to stop), not a faster callback.

### Scope is a real feature, not a thumb on the scale

ChronoTrace records only the target program's own code; the standard library and
site-packages are `DISABLE`d after one callback each (`recorder/scope.py`). The
tracing baselines have no such notion and trace everything. That difference is a
genuine, defensible property of the design — not a measurement trick — so the
ChronoTrace conditions are run scoped, exactly as the CLI runs them on a real
script. The workload deliberately calls pure-Python stdlib (`strptime`,
`statistics`) so this scoping has something real to exclude.

### `--sample` is not measured

The table has **no `--sample` row with a number in it.** `--sample` (record every
Nth hit of a hot line) does not exist yet — it is issue #18, deferred behind the
empty `.chrono` META block, see [ADR-0014](../docs/adr/0014-no-native-extension.md).
A planned feature is annotated as planned; it is never given a fabricated number.
This is the rule "a number you can't explain — do not publish it," applied to a
number that doesn't exist yet.

## Why medians and p95, never best-of-N

Best-of-N reports the single luckiest scheduling accident on the machine — the run
where no other process preempted, no interrupt landed, the CPU stayed boosted. A
user never gets that run reliably. The **median** reports what a typical run costs;
**p95** reports the tail a user occasionally hits. Best-of-N is how you flatter a
benchmark, so it is banned here. With small N, p95 sits close to the maximum sample;
that is stated rather than hidden.

## Why one fresh subprocess per sample

`sys.monitoring` is **process-global and sticky**, and both coverage.py and
ChronoTrace acquire monitoring tool ids and return `DISABLE` to de-instrument code
locations permanently (only `restart_events()` undoes it, for every tool at once).
Two conditions sharing a process would contaminate each other: a location
`DISABLE`d by one run stays disabled for the next, and a leftover tool id collides.
CPython's adaptive specialisation also warms code objects across runs, and
`settrace`/`sys.monitoring` interfere with each other. **One process per
measurement makes every number independent by construction.** The ~100 ms of
interpreter startup lands entirely outside the timed region, so it does not touch
the result.

## GC policy

GC is `collect()`ed once immediately before each timed region, then left **enabled**
for every condition. Disabling GC buys stability but flatters the allocation-heavy
capture condition — which is exactly the one whose realism matters most — so it
stays on. (The Day-2 spike disabled GC and documented that it *understated* the
allocating conditions; this suite does not repeat that trade.)

## The environment is in the artifact

Every generated run writes the machine, OS, Python version, ChronoTrace version and
git SHA, repetition count, warmup and GC policy into the top of RESULTS.md. A number
without its machine is meaningless. The exact CPU/RAM model is named in the curated
Environment section; the auto-captured row is what the machine's own `platform`
module reports, verbatim, so it cannot drift from reality.

## What this suite does *not* measure (and why that is stated, not hidden)

- **10M-event query latency and 10 GB-recording RSS.** Not measured on the dev
  machine — producing a 10 GB recording honestly needs a workload and a machine this
  suite does not pin. Quoting a projected number as measured would violate the rule
  above, so these are marked "not measured at this scale," not filled in.
- **UI scrubbing fps.** The frontend renders a fixed 256-bucket timeline and is a
  pure function of the current `seq` (ADR on the scrubber), so per-frame cost is
  O(1) in event count by construction; but a measured fps needs a headless GPU
  harness this suite does not have. The design bound is stated; an unmeasured fps
  number is not.
- **Linux and macOS.** All numbers are Windows-only. The Dockerfile pins a Linux
  environment for anyone reproducing on that platform; the numbers there will differ,
  particularly on timer resolution and scheduler behaviour.

## The observer effect

Recording changes timing. Every line callback, value capture and hash adds latency
that the un-recorded program never pays, so **a bug whose reproduction depends on
timing — a race condition, a deadlock window, an ordering hazard between threads —
may not reproduce under recording, or may reproduce differently.** This is not a bug
in ChronoTrace; it is a property of observing a running program, shared by every
tracer here (pdb and coverage perturb timing too). It is a real limitation of the
whole time-travel-debugging approach and it is documented in the README's "When not
to use ChronoTrace," not buried.

## Reproducing

```bash
python -m benchmarks            # regenerate the headline block (reps=5)
python -m benchmarks --reps 7   # more repetitions, tighter medians
python -m benchmarks --quick    # 2 workloads, reps=3 — the CI/nightly pass
python benchmarks/<bench>.py    # any single per-day detail bench
```

For a pinned environment, `benchmarks/Dockerfile` builds an image with the Python
version and dependencies fixed:

```bash
docker build -t chronotrace-bench -f benchmarks/Dockerfile .
docker run --rm chronotrace-bench      # runs `python -m benchmarks --quick`
```

Numbers should reproduce within a few percent across three runs on the same
machine; the softest cells are the sub-15 ms ones, where timer and scheduler noise
are a larger fraction of the measurement (called out in RESULTS.md where relevant).
