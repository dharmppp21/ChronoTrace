"""Day 18: grid-search the three storage knobs and settle them with data.

Run: `python benchmarks/bench_grid.py`  (writes benchmarks/plots/*.svg)

Phase 2 left block size, keyframe interval and compression level as knobs on purpose,
so today decides them by measurement. The objective is stated so the defaults are not
just numbers someone changes later for no reason:

    minimise random-access + reconstruction latency, subject to a file-size ceiling.

Scrubbing is the product; storage is cheap. So we buy read latency with bytes until the
file-size cost stops being worth it, and pick the knee of each curve.

The knobs interact only weakly, so the space is *sampled*, not exhausted: block x level
for size and write speed, block alone for read latency, interval alone for
reconstruction latency. Each recording is done once (recording is the slow part) and
reused across the sweep.
"""

from __future__ import annotations

import io
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

import random  # noqa: E402

from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402
from chronotrace.store import ChronoReader, apply, state_from_keyframe  # noqa: E402
from chronotrace.store.writer import ChronoWriter  # noqa: E402

_WORKLOADS = ("tight_loop", "fib_recursive", "json_pipeline", "io_bound")
_BLOCKS = (1024, 4096, 16384, 65536)
_LEVELS = (3, 9, 19)
_INTERVALS = (200, 1000, 5000, 25000)
_PLOTS = _ROOT / "benchmarks" / "plots"


def _record(name: str) -> tuple[list, list]:
    fn = WORKLOADS[name]
    fn()
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(include=["*"]))
    with recorder:
        fn()
    return sink.events, recorder._values._values


def _write(events: list, pool: list, *, block: int, level: int, interval: int) -> bytes:
    buf = io.BytesIO()
    writer = ChronoWriter(
        buf, block_events=block, keyframe_interval=interval, compression_level=level
    )
    for value in pool:
        writer.add_value(value)
    for event in events:
        writer.add(event)
    writer.close()
    return buf.getvalue()


def _median_us(fn, samples: list[int]) -> float:
    runs = []
    for seq in samples:
        t0 = time.perf_counter()
        fn(seq)
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs) * 1e6


def _reconstruct(reader: ChronoReader, target: int) -> object:
    kf = reader.nearest_keyframe_at_or_before(target)
    state = state_from_keyframe(kf) if kf else {}
    start = (kf.seq + 1) if kf else 0
    for delta in reader.deltas_between(start, target):
        state = apply(state, delta)
    return state


def _access_latency(reader: ChronoReader, n: int, rng: random.Random) -> float:
    samples = [rng.randrange(n) for _ in range(1500)]
    return _median_us(lambda s: reader[s], samples)


def _reconstruct_latency(reader: ChronoReader, n: int, rng: random.Random) -> float:
    samples = [rng.randrange(n) for _ in range(300)]
    return _median_us(lambda s: _reconstruct(reader, s), samples)


def _scrub_mevents_s(reader: ChronoReader, n: int) -> float:
    t0 = time.perf_counter()
    for seq in range(min(n, 100_000)):
        reader[seq]
    return min(n, 100_000) / (time.perf_counter() - t0) / 1e6


def sweep_block_level(recs: dict[str, tuple[list, list]]) -> dict:
    """Block x level -> bytes/event (averaged over workloads) and write Mevents/s."""
    print("\n== block x level: file size (B/event) and write throughput ==")
    bpe: dict[tuple[int, int], float] = {}
    for block in _BLOCKS:
        for level in _LEVELS:
            sizes, evs, secs = 0, 0, 0.0
            for events, pool in recs.values():
                t0 = time.perf_counter()
                data = _write(events, pool, block=block, level=level, interval=1000)
                secs += time.perf_counter() - t0
                sizes += len(data)
                evs += len(events)
            bpe[(block, level)] = sizes / evs
            print(
                f"  block={block:>6} level={level:>2}: {sizes / evs:6.2f} B/event  "
                f"write {evs / secs / 1e6:5.2f} Mevents/s"
            )
    return bpe


def sweep_block_access(events: list, pool: list) -> dict[int, tuple[float, float]]:
    """Block -> (random-access us, scrub Mevents/s) on a LARGE recording.

    A large recording is essential: a small file's blocks all fit the reader's LRU, so
    every random access is a cache hit and the block-size cost vanishes. On a recording
    bigger than the cache, a random jump is a cold block decode -- the real cost, which
    grows with block size.
    """
    print("\n== block: random-access latency and scrub throughput (tight_loop, large) ==")
    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    out: dict[int, tuple[float, float]] = {}
    for block in _BLOCKS:
        reader = ChronoReader.from_bytes(_write(events, pool, block=block, level=9, interval=1000))
        n = len(reader)
        access = _access_latency(reader, n, rng)
        scrub = _scrub_mevents_s(reader, n)
        out[block] = (access, scrub)
        print(f"  block={block:>6}: random access {access:6.1f} us   scrub {scrub:5.2f} Mevents/s")
    return out


def sweep_interval(events: list, pool: list) -> dict[int, tuple[float, float]]:
    """Interval -> (reconstruction us, file overhead %) at a SMALL block (json_pipeline).

    Reconstruction decodes the DELTAS blocks spanning the keyframe-to-target range, so a
    large block masks the interval entirely (one huge block decode dwarfs the deltas
    applied). Measuring at block=4096 lets the interval's real effect -- how many delta
    blocks and deltas a replay touches -- show through.
    """
    print("\n== interval: reconstruction latency and file overhead (json_pipeline, block=4096) ==")
    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    base = len(_write(events, pool, block=4096, level=9, interval=10 * len(events) + 1))
    out: dict[int, tuple[float, float]] = {}
    for interval in _INTERVALS:
        data = _write(events, pool, block=4096, level=9, interval=interval)
        reader = ChronoReader.from_bytes(data)
        recon = _reconstruct_latency(reader, len(reader), rng)
        overhead = (len(data) - base) / base * 100
        out[interval] = (recon, overhead)
        print(f"  interval={interval:>6}: reconstruct {recon:7.1f} us   file +{overhead:4.1f}%")
    return out


def main() -> int:
    print("recording four workloads (with value capture)...")
    recs = {name: _record(name) for name in _WORKLOADS}
    for name, (events, pool) in recs.items():
        print(f"  {name:<15} {len(events):>9,} events, {len(pool):>7,} values")

    bpe = sweep_block_level(recs)
    access = sweep_block_access(*recs["tight_loop"])  # large: blocks exceed the reader LRU
    interval = sweep_interval(*recs["json_pipeline"])

    _PLOTS.mkdir(exist_ok=True)
    _plot_block_size(bpe, access)
    _plot_interval(interval)
    print(f"\nplots written to {_PLOTS}")
    return 0


# --- SVG plotting (no matplotlib: the project ships no plotting dependency) ----------

_W, _H, _PAD = 640, 400, 64


def _axes(title: str, xlabel: str, ylabel: str) -> list[str]:
    mid = _W / 2
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W} {_H}" '
        'font-family="sans-serif">',
        f'<rect width="{_W}" height="{_H}" fill="white"/>',
        f'<text x="{mid}" y="24" text-anchor="middle" font-size="16" '
        f'font-weight="bold">{title}</text>',
        f'<text x="{mid}" y="{_H - 8}" text-anchor="middle" font-size="12">{xlabel}</text>',
        f'<text x="16" y="{_H / 2}" text-anchor="middle" font-size="12" '
        f'transform="rotate(-90 16 {_H / 2})">{ylabel}</text>',
        f'<line x1="{_PAD}" y1="{_H - _PAD}" x2="{_W - 20}" y2="{_H - _PAD}" stroke="black"/>',
        f'<line x1="{_PAD}" y1="30" x2="{_PAD}" y2="{_H - _PAD}" stroke="black"/>',
    ]


def _line(
    points: list[tuple[float, float]], xs: list[float], ys: list[float], colour: str
) -> list[str]:
    import math

    xmin, xmax = math.log10(min(xs)), math.log10(max(xs))
    ymax = max(ys) * 1.1

    def px(x: float) -> float:
        return _PAD + (math.log10(x) - xmin) / (xmax - xmin + 1e-9) * (_W - 20 - _PAD)

    def py(y: float) -> float:
        return (_H - _PAD) - y / ymax * (_H - _PAD - 30)

    pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in points)
    dots = "".join(
        f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="4" fill="{colour}"/>' for x, y in points
    )
    labels = "".join(
        f'<text x="{px(x):.1f}" y="{py(y) - 8:.1f}" text-anchor="middle" '
        f'font-size="10">{y:.0f}</text>'
        for x, y in points
    )
    xticks = "".join(
        f'<text x="{px(x):.1f}" y="{_H - _PAD + 16:.1f}" text-anchor="middle" '
        f'font-size="10">{int(x)}</text>'
        for x in xs
    )
    return [
        f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2"/>',
        dots,
        labels,
        xticks,
    ]


def _plot_block_size(bpe: dict, access: dict[int, tuple[float, float]]) -> None:
    blocks = list(_BLOCKS)
    ratio = [bpe[(b, 9)] for b in blocks]  # B/event at level 9
    lat = [access[b][0] for b in blocks]
    svg = _axes(
        "Block size vs size and random-access latency",
        "block (events, log)",
        "B/event (blue) · us (red)",
    )
    svg += _line(list(zip(blocks, ratio, strict=True)), blocks, ratio, "steelblue")
    svg += _line(list(zip(blocks, lat, strict=True)), blocks, lat, "firebrick")
    svg.append("</svg>")
    (_PLOTS / "block_size.svg").write_text("\n".join(svg), encoding="utf-8")


def _plot_interval(interval: dict[int, tuple[float, float]]) -> None:
    ivs = list(_INTERVALS)
    recon = [interval[i][0] for i in ivs]
    svg = _axes(
        "Keyframe interval vs reconstruction latency", "interval (events, log)", "reconstruct us"
    )
    svg += _line(list(zip(ivs, recon, strict=True)), ivs, recon, "seagreen")
    svg.append("</svg>")
    (_PLOTS / "keyframe_interval.svg").write_text("\n".join(svg), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
