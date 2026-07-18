"""The test that proves the crash guarantee: kill real recording processes at random
instants and assert every file still opens with a valid prefix.

This is the whole point of a debugger's storage layer -- a recording of a program that
crashed must survive the crash. It spawns a subprocess recording to a `.chrono`, kills
it (`SIGKILL` on POSIX, `TerminateProcess` on Windows -- `Popen.kill()` maps to both)
partway through the write, then opens the file and checks the recovered events are a
clean, seq-dense prefix. No block is ever half-decoded; the tail is simply gone.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from pathlib import Path

from chronotrace.store import ChronoReader, TruncatedRecording

_TARGET = Path(__file__).parent / "_recording_target.py"
# The crash-guarantee proof. Default is CI-friendly; set CHRONOTRACE_KILL_ITERS=100 for
# the full run described in the README. Each iteration spawns and kills a real process.
_ITERATIONS = int(os.environ.get("CHRONOTRACE_KILL_ITERS", "50"))


def _kill_after_random_progress(rng: random.Random, path: Path) -> None:
    """Spawn a recorder, wait until the file grows past a random size, then kill it.

    Waiting on a random *size* (not a random *time*) makes the kill land at a random,
    reproducible point in the write with real data on disk -- robust to how fast the
    machine records, which a fixed sleep is not.
    """
    # args are this test's own target script and a tmp path, not untrusted input:
    proc = subprocess.Popen([sys.executable, str(_TARGET), str(path), "6000"])  # noqa: S603
    try:
        threshold = rng.randint(100, 40_000)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # finished before we killed it: a clean, complete file
            if path.exists() and path.stat().st_size >= threshold:
                break
            time.sleep(0.003)
    finally:
        proc.kill()
        proc.wait()


def test_random_kills_all_recover_a_valid_prefix(tmp_path: Path) -> None:
    rng = random.Random(0)  # noqa: S311 -- reproducibility, not security
    recovered_any = False
    for i in range(_ITERATIONS):
        path = tmp_path / f"rec_{i}.chrono"
        _kill_after_random_progress(rng, path)

        try:
            reader = ChronoReader.open(path)
        except TruncatedRecording:
            continue  # killed before a single block reached disk: nothing to recover, valid
        with reader:
            seqs = [e.seq for e in reader.iter_events()]
        # The guarantee: whatever survived is a clean seq-dense prefix -- never a partial
        # event, never a crash, never a hang. The tail is lost, not corrupt.
        assert seqs == list(range(len(seqs))), (
            f"iteration {i}: recovered a non-prefix {seqs[:5]}..."
        )
        recovered_any = recovered_any or bool(seqs)
    assert recovered_any, "no iteration recovered any events -- the kill window is wrong"
