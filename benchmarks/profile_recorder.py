"""Profile the recorder hot path and the pipeline. MEASURE, do not optimise (day 40).

Three tools, because each answers a different question and one lies where it matters:

* **cProfile** -- deterministic call counts + tottime, but its per-call instrumentation
  cost distorts a *per-line* hot path this tight: it perturbs exactly the callbacks we
  are trying to weigh. Useful for call *counts*, not for time *shares* here.
* **py-spy** (primary) -- a sampling profiler, no instrumentation, so it does not distort
  the callbacks; it produces flamegraphs and can later profile a native extension too.
* **micro-timers** -- `perf_counter_ns` around the hot functions in isolation, to answer
  "which half of capture()" that a flamegraph blurs into one wide bar.

Run:
  python benchmarks/profile_recorder.py micro
  python benchmarks/profile_recorder.py cprofile json_pipeline
  python benchmarks/profile_recorder.py gc json_pipeline
  python benchmarks/profile_recorder.py flamegraphs        # writes flamegraphs/*.svg via py-spy
  python benchmarks/profile_recorder.py record tight_loop  # a bare py-spy sampling target
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import pstats
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

from workloads import WORKLOADS  # noqa: E402

from chronotrace.recorder import MemorySink, Recorder  # noqa: E402

_ORDER = ("tight_loop", "fib_recursive", "json_pipeline", "io_bound")
_FLAME_DIR = _ROOT / "benchmarks" / "flamegraphs"


def _record_once(name: str) -> Recorder:
    rec = Recorder(MemorySink(), capture_values=True)
    with rec:
        WORKLOADS[name]()
    return rec


def cmd_record(args: argparse.Namespace) -> int:
    """A bare py-spy sampling target: record `workload` `--repeat` times, no output."""
    for _ in range(args.repeat):
        _record_once(args.workload)
    return 0


def cmd_flamegraphs(args: argparse.Namespace) -> int:
    """py-spy over each workload -> a flamegraph SVG.

    Sampling does not distort the per-line callbacks the way cProfile would, which is why it is
    the primary tool.
    """
    _FLAME_DIR.mkdir(parents=True, exist_ok=True)
    for name in _ORDER:
        out = _FLAME_DIR / f"{name}.svg"
        print(f"py-spy -> {out.name} ...", flush=True)
        cmd = [
            "py-spy",
            "record",
            "--format",
            "flamegraph",
            "--rate",
            "300",
            "-o",
            str(out),
            "--",
            sys.executable,
            str(Path(__file__)),
            "record",
            name,
            "--repeat",
            str(args.repeat),
        ]
        subprocess.run(cmd, check=True)  # noqa: S603
    return 0


def cmd_cprofile(args: argparse.Namespace) -> int:
    """Deterministic call counts. tottime is INFLATED here -- read counts, not shares."""
    pr = cProfile.Profile()
    rec = Recorder(MemorySink(), capture_values=True)
    pr.enable()
    with rec:
        WORKLOADS[args.workload]()
    pr.disable()
    print(f"# cProfile of recording {args.workload}: the profiler's per-call cost inflates a")
    print("# per-line hot path -- trust py-spy for time shares; use these for call counts.")
    pstats.Stats(pr).sort_stats("tottime").print_stats(18)
    return 0


_SCALAR = 424242
_RECORD = {"id": 7, "hour": 3, "region": "emea", "amount": 213}
_LIST = [{"id": i, "hour": i % 24, "region": "emea", "amount": i} for i in range(200)]


def _ns_per_op(fn: Callable[[], Any], n: int) -> float:
    """Median ns/op over 5 batches of `n` calls, warmed."""
    fn()
    fn()
    batches = []
    for _ in range(5):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            fn()
        batches.append((time.perf_counter_ns() - t0) / n)
    batches.sort()
    return batches[2]


def cmd_micro(_args: argparse.Namespace) -> int:
    """capture() vs digest() in isolation.

    The flamegraph says 'capture is hot'; this says which half, on the value shapes the workloads
    actually capture.
    """
    from chronotrace.recorder.capture import capture
    from chronotrace.recorder.dedup import digest

    print(f"{'value shape':<18}{'capture ns':>12}{'digest ns':>12}{'sum ns':>10}{'digest%':>9}")
    print("-" * 61)
    for label, val, n in (
        ("int", _SCALAR, 200_000),
        ("4-key dict", _RECORD, 50_000),
        ("200-record list", _LIST, 2_000),
    ):
        cap = _ns_per_op(lambda v=val: capture(v), n)
        captured = capture(val)
        dig = _ns_per_op(lambda c=captured: digest(c), n)
        print(f"{label:<18}{cap:>12.0f}{dig:>12.0f}{cap + dig:>10.0f}{dig / (cap + dig):>8.0%}")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """GC pressure + memory growth over one recording -- pathologies a flamegraph hides."""
    name = args.workload
    WORKLOADS[name]()
    gc.collect()
    before = [s["collections"] for s in gc.get_stats()]
    tracemalloc.start()
    rec = _record_once(name)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    after = [s["collections"] for s in gc.get_stats()]
    events = len(rec.sink.events)  # type: ignore[attr-defined]
    gens = [a - b for a, b in zip(after, before, strict=True)]
    print(f"{name}: {events:,} events")
    print(f"  gc collections during record (gen0/1/2): {gens}")
    print(f"  tracemalloc peak: {peak / 1e6:.1f} MB, {peak / max(events, 1):.0f} B/event")
    return 0


_DISPATCH = {
    "record": cmd_record,
    "flamegraphs": cmd_flamegraphs,
    "cprofile": cmd_cprofile,
    "micro": cmd_micro,
    "gc": cmd_gc,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile the recorder (day 40).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in _DISPATCH:
        sp = sub.add_parser(name)
        if name in ("record", "cprofile", "gc"):
            sp.add_argument("workload", choices=_ORDER)
        if name in ("record", "flamegraphs"):
            sp.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    return _DISPATCH[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
