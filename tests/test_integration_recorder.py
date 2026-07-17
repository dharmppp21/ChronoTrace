"""End-to-end: the eight recorder pieces working together on real programs.

The unit tests prove each part in isolation; this proves the assembly. It records
all five example programs through the *real* default scope (not a test-only
injection), so it also checks that scope filtering keeps the stdlib out of the
timeline and that redaction fires in the full pipeline. The invariants asserted
here are the same ones every later phase will assert about recordings loaded from
a file -- imported from the shared library rather than re-stated.
"""

from __future__ import annotations

import gc
import importlib
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from chronotrace.recorder import Event, EventKind, MemorySink, Recorder
from chronotrace.recorder.redact import REDACTED
from chronotrace.recorder.scope import Scope
from tests.recorder.invariants import (
    assert_every_frame_dies_once,
    assert_frame_lifecycles_are_well_formed,
    assert_seq_is_a_total_order,
)

_EXAMPLES = Path(__file__).parent.parent / "examples"
_ALL = ("simple", "exceptions", "generators", "pipeline_realistic", "buggy_pipeline")


def _record_main(module_name: str) -> tuple[list[Event], Recorder]:
    """Record `module.main()` scoped to the examples/ directory."""
    sys.path.insert(0, str(_EXAMPLES))
    try:
        module: Any = importlib.import_module(module_name)
        sink = MemorySink()
        rec = Recorder(sink, scope=Scope(roots=[str(_EXAMPLES)]))
        with rec:
            module.main()
        return sink.events, rec
    finally:
        sys.path.remove(str(_EXAMPLES))


def _files(events: list[Event], rec: Recorder) -> set[str]:
    return {rec._codes.resolve(e.code_id).co_filename for e in events}


@pytest.mark.parametrize("name", _ALL)
def test_example_records_a_coherent_stream(name: str) -> None:
    """Every example records, keeps seq a total order, and stays in scope."""
    events, rec = _record_main(name)
    assert events, f"{name} recorded nothing"
    assert_seq_is_a_total_order(events)

    files = _files(events, rec)
    assert files, "sanity: something has a code object"
    leaked = {f for f in files if not f.startswith(str(_EXAMPLES))}
    assert not leaked, f"scope leaked outside examples/: {leaked}"
    assert not any("chronotrace" in f for f in files), "recorded our own code"


@pytest.mark.parametrize("name", _ALL)
def test_example_frames_are_balanced(name: str) -> None:
    """Every frame that is born dies exactly once -- the registry never leaks."""
    if name == "generators" and sys.version_info < (3, 13):
        pytest.skip("abandoned generator leaks a frame on CPython <3.13 (see test_generators)")
    events, _ = _record_main(name)
    assert_frame_lifecycles_are_well_formed(events)
    assert_every_frame_dies_once(events)


def test_redaction_fires_in_the_full_pipeline() -> None:
    """A secret-named local is redacted end to end, not just in the unit test."""

    def leaky() -> int:
        auth_token = "sk-live-do-not-record"  # noqa: S105
        balance = 100
        return balance + len(auth_token)

    sink = MemorySink()
    rec = Recorder(sink, capture_values=True, scope=Scope(roots=[str(Path(__file__).parent)]))
    with rec:
        leaky()

    name_id = rec._names.intern("auth_token")
    redacted = [e for e in sink.events if e.kind is EventKind.VAR_WRITE and e.name_id == name_id]
    assert redacted, "the secret variable should still appear in the timeline"
    assert all(rec._values.resolve(e.value_ref) == REDACTED for e in redacted if e.value_ref)


def _million_events() -> int:
    total = 0
    for i in range(260_000):
        a = i + 1
        b = a * 2
        total += b
    return total


def test_one_million_events_stay_under_the_memory_ceiling() -> None:
    """1M events in MemorySink must fit a stated budget.

    Flow-only (no value pool) so this measures the event stream itself -- the
    standing budget the day-4 event model was sized against. If it ever fails,
    that is Phase 2's justification (the file store), so the number is recorded in
    benchmarks/RESULTS.md either way. tracemalloc, not RSS: it is stdlib and
    cross-platform, and it measures Python allocation, which is what we control.
    """
    budget_mb = 400.0  # measured ~230 MB on the dev machine; see RESULTS.md

    gc.collect()
    tracemalloc.start()
    sink = MemorySink()
    with Recorder(sink, capture_values=False, scope=Scope(roots=[str(Path(__file__).parent)])):
        _million_events()
    count = len(sink.events)
    peak_mb = tracemalloc.get_traced_memory()[1] / 1e6
    tracemalloc.stop()

    assert count >= 1_000_000, f"expected >=1M events, got {count:,}"
    assert peak_mb < budget_mb, f"1M events peaked at {peak_mb:.0f} MB (budget {budget_mb:.0f} MB)"
