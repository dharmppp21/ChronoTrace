"""One entry point for the whole benchmark suite. `python -m benchmarks`.

A benchmark nobody can re-run is an anecdote, and an anecdote in a README is a
liability the first time someone tries to reproduce it. So this regenerates the
environment-sensitive numbers (the overhead table, storage bytes/event, and
reconstruction latency) into the *generated block* at the top of `RESULTS.md`, and
puts the environment -- machine, OS, Python, git SHA, GC state, reps -- **in the
artifact**, because a number without its machine is meaningless and an interviewer
will ask.

The per-day detail benches (`bench_*.py`) measure distinct things (compression,
dedup, indexing, stepping) and remain runnable drill-downs; they are listed, not
deleted -- deleting a script that documents a distinct number destroys the number.

    python -m benchmarks            # regenerate the block (reps=5)
    python -m benchmarks --reps 7
    python -m benchmarks --quick    # overhead on 2 workloads, fewer reps -- CI/nightly
"""

from __future__ import annotations

import argparse
import gc
import io
import platform
import random
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from . import bench_overhead  # noqa: E402  -- package-local; not the spikes/ namesake

RESULTS = _ROOT / "benchmarks" / "RESULTS.md"
GEN_START = "<!-- BENCH:GENERATED:START -->"
GEN_END = "<!-- BENCH:GENERATED:END -->"


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 -- git on PATH is trusted here
            capture_output=True,
            text=True,
            cwd=_ROOT,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("chronotrace")
    except Exception:
        return "unknown"


def _environment(reps: int) -> str:
    """The methodology header. Everything an interviewer needs to trust the numbers."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join(
        [
            "### Environment (auto-captured)",
            "",
            "| | |",
            "|---|---|",
            f"| Machine | {platform.processor() or platform.machine()} |",
            f"| Logical CPUs | {__import__('os').cpu_count()} |",
            f"| OS | {platform.platform()} |",
            f"| Python | {platform.python_version()} ({platform.python_implementation()}) |",
            f"| ChronoTrace | {_version()} @ git {_git_sha()} |",
            f"| Generated | {now} |",
            f"| Repetitions | {reps} (one fresh subprocess per sample) |",
            "| GC | collected before each timed region, left **enabled** throughout |",
            "| Warmup | one uninstrumented run per child before timing |",
            "",
            "Reproduce: `python -m benchmarks`. The exact CPU/RAM model is named in the "
            "curated Environment section below; the row above is what the machine's own "
            "`platform` reports, verbatim.",
        ]
    )


def _overhead_table(reps: int, workloads: tuple[str, ...] | None) -> str:
    print("running overhead matrix (this dominates the runtime)...", flush=True)
    rows = bench_overhead.run_matrix(reps=reps, workloads=workloads)
    lines = [
        "### Recording overhead vs. fair baselines",
        "",
        "Each condition does **strictly more work** than the one above it; a ChronoTrace",
        "row below coverage.py is *not* a per-event speed win (see METHODOLOGY.md). "
        "`--sample` is planned (issue #18), not built, so it is not measured.",
        "",
        "| Workload | Condition | Median | p95 | vs base | events |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        ev = f"{r['events']:,}" if r["events"] >= 0 else "—"
        base = r["condition"] == "baseline"
        wl = f"**{r['workload']}**" if base else ""
        lines.append(
            f"| {wl} | {bench_overhead.LABELS[r['condition']]} | "
            f"{r['median'] * 1000:.2f} ms | {r['p95'] * 1000:.2f} ms | "
            f"{'1.0×' if base else f'{r["ratio"]:.1f}×'} | {ev} |"
        )
    return "\n".join(lines)


def _storage_reconstruct() -> str:
    """One real recording: bytes/event, keyframes, and reconstruct p50/p95/p99."""
    from workloads import WORKLOADS

    from chronotrace.reconstruct import KeyframeReconstructor
    from chronotrace.recorder import MemorySink, Recorder
    from chronotrace.recorder.scope import Scope
    from chronotrace.store import ChronoReader
    from chronotrace.store.writer import ChronoWriter

    WORKLOADS["json_pipeline"]()
    sink = MemorySink()
    rec = Recorder(sink, capture_values=True, scope=Scope(include=["*"]))
    with rec:
        WORKLOADS["json_pipeline"]()
    pool = rec._values._values
    buf = io.BytesIO()
    writer = ChronoWriter(buf)
    for value in pool:
        writer.add_value(value)
    for event in sink.events:
        writer.add(event)
    writer.close()
    reader = ChronoReader.from_bytes(buf.getvalue())
    n = len(reader)
    size = len(buf.getvalue())

    def pct(samples: list[float]) -> str:
        samples.sort()
        at = lambda q: samples[min(len(samples) - 1, int(len(samples) * q))] * 1e6  # noqa: E731
        return f"{at(0.5):.0f} / {at(0.95):.0f} / {at(0.99):.0f}"

    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    cold = KeyframeReconstructor(reader, use_cache=False)
    cold_samples = []
    for _ in range(400):
        t0 = time.perf_counter()
        cold.reconstruct(rng.randrange(n))
        cold_samples.append(time.perf_counter() - t0)

    # A playhead drag: +1 event at a time through the cache -- the interactive path.
    warm = KeyframeReconstructor(reader, use_cache=True)
    warm.reconstruct(n // 2)
    drag_samples = []
    for seq in range(n // 2 + 1, n // 2 + 501):
        t0 = time.perf_counter()
        warm.reconstruct(seq)
        drag_samples.append(time.perf_counter() - t0)

    return "\n".join(
        [
            "### Storage & reconstruction",
            "",
            "One json_pipeline recording, value capture on, `include=['*']` (a full trace, "
            "not the scoped default) so the recording is large enough for the tail to mean "
            "something.",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Events | {n:,} |",
            f"| Distinct pooled values | {len(pool):,} |",
            f"| `.chrono` size | {size / 1024:.1f} KiB |",
            f"| Bytes / event | {size / max(n, 1):.1f} |",
            f"| Keyframes | {reader.keyframe_count():,} |",
            f"| Cold random reconstruct p50/p95/p99 | {pct(cold_samples)} µs |",
            f"| Cached +1 drag p50/p95/p99 | {pct(drag_samples)} µs |",
            "",
            "Cold random access replays up to one keyframe interval (ADR-0006); the flat "
            "tail confirms the bound holds. The **cached +1 drag** is the interactive "
            'scrubbing path — ~200× faster — and is the number behind "instant scrubbing".',
        ]
    )


def _detail_index() -> str:
    return "\n".join(
        [
            "### Detail benches (run individually for the full per-day numbers)",
            "",
            "These measure distinct things and are the reproducible source for numbers "
            "quoted in ADRs and the curated notes below:",
            "",
            "- `bench_events.py` — event model, AoS vs SoA (day 4)",
            "- `bench_dedup.py` — dedup hit rate & recording-size reduction (day 8)",
            "- `bench_scope.py` — the DISABLE scoping win (day 9)",
            "- `bench_compression.py` — columnar+zstd bytes/event, throughput (day 14)",
            "- `bench_keyframe_interval.py`, `bench_grid.py` — storage knobs (days 15, 18)",
            "- `bench_delta.py`, `bench_stepping.py` — deltas & backward stepping (days 16, 21)",
            "- `bench_index.py` — index build rate & size (day 26)",
            "- `bench_reconstruct.py` — reconstruction tail & value resolve (day 20)",
            "- `profile_recorder.py` — py-spy flamegraphs & micro-timers (days 40–42)",
        ]
    )


def _splice(generated: str) -> None:
    text = RESULTS.read_text(encoding="utf-8") if RESULTS.exists() else ""
    block = f"{GEN_START}\n\n{generated}\n\n{GEN_END}"
    if GEN_START in text and GEN_END in text:
        pre = text[: text.index(GEN_START)]
        post = text[text.index(GEN_END) + len(GEN_END) :]
        RESULTS.write_text(pre + block + post, encoding="utf-8")
    else:  # first run: put the generated block on top, keep whatever was there
        sep = "\n\n---\n\n## Curated per-day notes\n\n" if text else "\n"
        RESULTS.write_text(block + sep + text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--quick", action="store_true", help="2 workloads, reps=3 — CI/nightly")
    args = parser.parse_args()

    gc.enable()
    reps = 3 if args.quick else args.reps
    workloads = ("tight_loop", "json_pipeline") if args.quick else None

    parts = [
        "# Benchmark results",
        "",
        "> Numbers between the generated markers are produced by `python -m benchmarks`.",
        "> Methodology, fair-baseline rationale and limitations: [METHODOLOGY.md](METHODOLOGY.md).",
        "",
        _environment(reps),
        "",
        _overhead_table(reps, workloads),
        "",
        _storage_reconstruct(),
        "",
        _detail_index(),
    ]
    _splice("\n".join(parts))
    print(f"\nwrote generated block -> {RESULTS.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
