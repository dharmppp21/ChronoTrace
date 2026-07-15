# ADR-0001: Recording strategy — omniscient capture vs. deterministic replay

- **Status:** **DRAFT — Context only.** Decision lands day 3, once value-capture
  cost is measured. Do not act on this document yet.
- **Date:** 2026-07-15 (context), decision pending
- **Deciders:** dharmppp21

## Context

ChronoTrace must let a developer reach any past instant of a program's execution.
There are two families of ways to do that, and the choice is effectively
irreversible: it determines the storage format, the query engine, and whether
random access is possible at all.

This section records what we have **measured**, not what we assume.

### Measured: line-observation overhead (day 2, `spikes/RESULTS-overhead.md`)

Machine: i5-13450HX, Windows 11 build 26200, Python 3.14.3, medians of 7 reps,
one fresh subprocess per sample.

| Workload | baseline | monitoring LINE + append | scoped (DISABLE out-of-scope) |
|---|---:|---:|---:|
| tight_loop (worst case) | 8.42 ms | **12.4×** | 15.8× |
| fib_recursive | 4.98 ms | 9.1× | 10.8× |
| json_pipeline (realistic) | 7.08 ms | 4.9× | **1.5×** |
| io_bound | 160.50 ms | 1.0× | 1.0× |

Three findings carry into the decision:

1. **`sys.monitoring` is not inherently faster than `sys.settrace`.** On the one
   cleanly-matched comparison (`tight_loop`, near-identical event counts), settrace
   won 7.2× vs 8.0×. PEP 669's value is structural — it can be told to stop — not a
   cheaper per-event path. Any claim that ChronoTrace is fast "because it uses the
   modern API" would be false.
2. **Scoping via `DISABLE` is worth 3.3× on realistic code** (json_pipeline 4.9× →
   1.5×, 93% fewer events) **and costs 19–27% on code that is entirely in scope**,
   where the per-event `co_filename` check (~37 ns/event) buys nothing. The scope
   decision must therefore be cached per code object, or avoided entirely by
   instrumenting only in-scope code objects via `set_local_events`.
3. **Instrumentation cost tracks Python lines executed, not wall time.** I/O-bound
   programs are free to record.

**Caveat that bounds all of the above:** these callbacks are trivial (count, or
append a tuple), and GC was disabled around the timed region — which specifically
understates the append condition, since 750k tuple allocations are exactly what
would trigger collection. **Today's numbers are a floor, not the answer.**

### Not yet measured: value capture (day 3)

The floor above says observing *control flow* is affordable. The open question is
whether capturing *values* — the thing that makes time travel useful rather than a
line-number movie — is affordable on top. Day 3 measures it, with GC enabled, and
this ADR's decision waits for that number.

### The two options

**Omniscient recording** — capture state as it happens, write it all down. Gives
random access by construction, at the price of overhead and file size.

**Deterministic replay** (the `rr` approach) — record only sources of
nondeterminism, then re-execute to reach a past instant. Tiny recordings, low
overhead, but in pure Python you must intercept every source of nondeterminism
(time, randomness, I/O, threads, hash seeds, C extensions), and reaching instant N
means re-executing from the start.

## Decision

*Pending day 3.*

## Alternatives considered

*Pending day 3.*

## Consequences

*Pending day 3.*
