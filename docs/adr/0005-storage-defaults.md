# ADR-0005: Storage defaults, chosen by measurement

- **Status:** Accepted
- **Date:** 2026-07-19
- **Deciders:** dharmppp21
- **Supersedes / Superseded by:** tunes the knobs ADR-0004 deliberately left open

## Context

Phase 2 shipped three knobs as knobs — **block size**, **keyframe interval**, and
**compression level** — precisely so this decision could settle them with data instead
of taste. A default without a stated objective is just a number someone changes later
for no reason, so the objective comes first:

> **Minimise random-access + reconstruction latency, subject to a file-size ceiling of
> roughly +10% over the compression optimum.**

Scrubbing is the product; storage is cheap. So we buy read latency with bytes until the
size cost stops being worth it, and take the knee of each curve. Measured by
`benchmarks/bench_grid.py` on the four Day-2 workloads (median of the sweep;
`benchmarks/plots/*.svg` are the curves).

## Decision

| Knob | Old | **Chosen** | Why |
|---|---:|---:|---|
| `block_events` | 65536 | **4096** | random access is 15× faster (133 ms → 8.7 ms) for +9% file |
| `keyframe_interval` | 1000 | **1000** | 2.7 ms reconstruction at +4% file — the knee |
| `compression_level` | 9 | **9** | the ratio/speed knee; the recorder compresses on its hot path |

### Block size — the headline change

Random access decodes a **whole block**, so its latency grows ~linearly with block size
(measured ~2.1 µs/event on a recording larger than the reader's LRU):

| block | random access | file |
|---:|---:|---:|
| 1024 | 2.2 ms | 5.80 B/event |
| **4096** | **8.7 ms** | **5.01 B/event** |
| 16384 | 34 ms | 4.68 B/event |
| 65536 | 133 ms | 4.58 B/event |

The old 65536 default was a *compression* optimum that made *scrubbing* unusable
(133 ms per cold jump). 4096 is the knee: it costs only +9% file over the 65536 optimum
and is well inside a 16 ms interactive-frame budget. Going to 1024 buys 4× more speed
for another +16% file — a steepening cost for diminishing return.

*(A subtle trap, called out in the benchmark: on a recording small enough to fit the
reader's 8-block LRU, every access is a cache hit and block size looks free — a first
run showed 65536 at 0.6 µs. The real cost only appears on a recording larger than the
cache. Measure on data that exceeds the cache, or you will ship the wrong default.)*

### Keyframe interval

At block 4096, reconstruction (nearest keyframe + apply deltas) trades against file
overhead: 200 → 2.5 ms at +20%, **1000 → 2.7 ms at +4%**, 5000 → 5.0 ms at +0.8%,
25000 → 12.6 ms at +0.2%. 1000 is the knee — reconstruction stays ~2.7 ms while overhead
drops to a modest 4%.

**A finding worth stating:** measured first at block 65536, the interval curve was
*flat* (~42 ms at every interval). The interval was not inert — reconstruction was
dominated by decoding one huge DELTAS block (sized by `block_events`), which dwarfed the
≤interval deltas actually applied. Shrinking the block to 4096 unmasked the interval's
real effect. This is why `block_events` is the dominant latency knob and the interval is
secondary; a future optimisation (size DELTAS blocks by keyframe span) would sharpen the
interval further, and is tracked, not hidden.

### Compression level

Level 9 gets ~4.58 B/event at 0.17 Mevents/s write; level 19 gets 4.21 (−8%) at 0.08
Mevents/s (2× slower); level 3 gets 5.28 (+15%) at 0.19. The recorder compresses on the
traced program's own thread, so a 2× write penalty for 8% size is a bad trade. 9 is the
knee. Kept as a per-writer knob so an offline archival re-compression can choose 19.

## What was deleted, and what was measured and kept

Deleting your own optimisation on evidence is a strong signal — so is *keeping* on
evidence rather than habit:

- **Trained zstd dictionary — deleted** (Day 14, ADR-0004 lineage): measured net-negative
  on real block sizes (−7 KB on a single VALUES block; the block self-contextualises
  through zstd's own window). It is gone.
- **The three integer codecs — measured, all kept.** Across real EVENTS blocks the winner
  is `rle` 60%, `raw` 30%, `delta-rle` 10% — and delta-rle, though chosen least, crushes
  the monotonic `seq` column to ~6 KB total. Each earns its place on a different column;
  none is removed.

## Consequences

**What this buys:** a **15× faster random access** (the product's core interaction) for a
9% storage cost, and a reconstruction latency (~2.7 ms) comfortably inside a scrubbing
frame budget — all justified by curves, not a blog post.

**What this costs:** ~4× more blocks per recording (more index entries, more per-block
framing), which the benchmarks confirm is negligible (the index is still a few KB, open
is still O(blocks) at ~KB heap).

**Reversal trigger:** revisit `block_events` if Phase 3's reconstruct layer profiles cold
random access below 5% of scrub time (then trade back toward compression), or if
per-block framing overhead exceeds 2% of file size at 4096.
