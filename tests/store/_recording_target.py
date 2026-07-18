"""Not a test -- a subprocess target for test_crash_real.py.

It records its own workload to a `.chrono` file with a small block size, so blocks flush
to the OS steadily and there is a wide window to be killed mid-write. `test_crash_real`
spawns it, kills it at a random instant, and asserts the file still opens with a valid
prefix. Run directly: `python _recording_target.py <path> [iterations]`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Importable when chronotrace is installed (CI); fall back to the source tree otherwise.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from chronotrace.recorder import Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.store.writer import FileSink


def workload(n: int) -> int:
    total = 0
    window: list[int] = []
    for i in range(n):
        total += i * i
        window.append(total % 97)
        if len(window) > 50:
            window.pop(0)
    return total


def main() -> None:
    path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    sink = FileSink(path, block_events=256)  # small blocks: flush often, wide kill window
    # Flow-only (no value capture): the crash test is about recovering *blocks*, not
    # values, and flow-only records fast enough to spawn many times.
    with Recorder(sink, capture_values=False, scope=Scope(include=["*"])):
        workload(n)


if __name__ == "__main__":
    main()
