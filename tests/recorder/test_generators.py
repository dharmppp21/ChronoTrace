"""Generators and coroutines: the frames that broke the stack."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from chronotrace.recorder import EventKind, MemorySink, Recorder
from chronotrace.recorder.frames import FrameRegistry

from .conftest import EXAMPLES, OnlyThisFile, record_example
from .invariants import (
    assert_every_frame_dies_once,
    assert_frame_lifecycles_are_well_formed,
    assert_seq_is_a_total_order,
)


def _record_example(func_name: str) -> list[Any]:
    return record_example("generators", func_name)[0]


def test_pipeline_frames_are_balanced() -> None:
    events = _record_example("pipeline")
    assert_frame_lifecycles_are_well_formed(events)
    assert_every_frame_dies_once(events)
    assert_seq_is_a_total_order(events)


def test_generator_keeps_one_frame_id_across_its_whole_life() -> None:
    """The rule the registry exists to enforce.

    A generator entered at CALL, left at YIELD, re-entered at RESUME, and died at
    RETURN is ONE frame. Assigning a fresh id per RESUME would split it into N
    unrelated frames and make day 27's call tree describe a program that never ran.
    """
    events = _record_example("pipeline")
    numbers_events = [e for e in events if e.kind in {EventKind.CALL, EventKind.RESUME}]
    assert numbers_events, "sanity: generators were entered"

    # every RESUME must reuse an id that some CALL already introduced
    called = {e.frame_id for e in events if e.kind is EventKind.CALL}
    resumed = {e.frame_id for e in events if e.kind is EventKind.RESUME}
    assert resumed <= called, f"RESUME invented new frame ids: {resumed - called}"


def test_yield_suspends_but_does_not_kill() -> None:
    """YIELD leaves execution; it does not end the frame.

    If YIELD killed the frame, the following RESUME would have nothing to recover
    and the generator would fragment.
    """
    events = _record_example("pipeline")
    yielded = {e.frame_id for e in events if e.kind is EventKind.YIELD}
    resumed = {e.frame_id for e in events if e.kind is EventKind.RESUME}
    assert yielded & resumed, "a yielded frame must be resumable"


def test_two_generators_of_one_code_object_stay_distinct() -> None:
    """The counter-example that killed the stack.

    Two live generators of the same function interleave: F0 yields, F1 starts, F0
    resumes. Distinct frames sharing one code object must never be fused.
    """
    events = _record_example("interleaved_generators")
    assert_frame_lifecycles_are_well_formed(events)
    assert_every_frame_dies_once(events)

    numbers_calls = [e for e in events if e.kind is EventKind.CALL]
    frame_ids = [e.frame_id for e in numbers_calls]
    assert len(frame_ids) == len(set(frame_ids)), "two generators were fused into one frame"


@pytest.mark.xfail(
    sys.version_info < (3, 13),
    reason=(
        "CPython < 3.13 does not emit a sys.monitoring PY_UNWIND when a generator is "
        "finalised by garbage collection (GeneratorExit); the event stream sees the "
        "CALL but no death, so the registry leaks the abandoned generator's frame on "
        "3.12. Fixed in CPython 3.13. Frames are not weakref-able and no event fires, "
        "so it is unfixable at the recorder level on 3.12 -- documented, not silently "
        "wrong. strict=True flags this the moment 3.12 starts emitting the event."
    ),
    strict=True,
)
def test_abandoned_generator_still_dies() -> None:
    """A generator dropped before exhaustion must not leak a live frame.

    On CPython >= 3.13, GeneratorExit thrown during collection unwinds the frame
    and PY_UNWIND fires, so the frame dies. If this ever fails there, the registry
    leaks one frame per abandoned generator and day 27's call tree grows phantom
    nodes that never exit. On 3.12 the event is not emitted at all (see the xfail).
    """
    events = _record_example("abandoned_generator")
    assert_every_frame_dies_once(events)
    assert any(e.kind is EventKind.UNWIND for e in events), (
        "the abandoned generator should exit via GeneratorExit unwind"
    )


def test_async_gather_interleaves_and_stays_coherent() -> None:
    """Coroutines are generators underneath; await suspends exactly as yield does.

    Several frames are suspended at once and resume out of order. No per-frame
    structure can order that -- only seq can, which is why it is a global clock.
    """
    events = _record_example("async_gather")
    assert_frame_lifecycles_are_well_formed(events)
    assert_seq_is_a_total_order(events)

    resumed = [e.frame_id for e in events if e.kind is EventKind.RESUME]
    assert len(set(resumed)) >= 2, "expected several coroutine frames to resume"


def test_a_start_never_inherits_a_live_frames_id() -> None:
    """Address reuse must not fuse two frames -- the bug `frame_id` exists to prevent.

    A `PY_START` for an address the registry still holds proves the previous owner died
    without an exit event and CPython reused its address (which happens for real on 3.12,
    where an abandoned generator emits no `PY_UNWIND` -- issue #4). Recovering that id
    would give a brand-new frame a dead frame's identity, and the delta stream would then
    carry two `FRAME_ENTER`s for one live frame.

    Found by the day-22 equivalence harness. Driven directly rather than through a
    recording because provoking real address reuse is a coin flip.
    """
    registry = FrameRegistry()
    frame = sys._getframe()
    first = registry.enter(frame)
    assert registry.enter(frame, resuming=True) == first, "a resume must recover its id"
    assert registry.enter(frame) != first, "a start must never inherit a live frame's id"


def test_registry_is_empty_after_a_recording() -> None:
    """The debugging checklist's first line: a non-empty registry means a missing exit path.

    Every shape `generators.main()` exercises, except -- below 3.13 -- the abandoned one.
    CPython < 3.13 emits no `PY_UNWIND` when the collector finalises an abandoned
    generator (see `test_abandoned_generator_still_dies`' xfail), so that frame is
    unobservably dead and the registry keeps it. Worse, *whether* it is still held when
    `live_count` is read depends on when the collector ran: this asserted `== 0` over
    `main()` for weeks and passed by luck, alternating 1/0/1/0 between consecutive
    recordings in one process, until an unrelated test changed the parity.

    So the one shape the interpreter cannot report is excluded on the interpreter that
    cannot report it, and the invariant stays **exact** everywhere rather than being
    weakened to `<= 1` on every platform to accommodate one. Tracked as issue #4.
    """
    sys.path.insert(0, str(EXAMPLES))
    try:
        import generators  # type: ignore[import-not-found]

        workloads = [
            generators.pipeline,
            generators.interleaved_generators,
            generators.async_gather,
        ]
        if sys.version_info >= (3, 13):
            workloads.append(generators.abandoned_generator)
        rec = Recorder(MemorySink(), scope=OnlyThisFile(generators.__file__))
        with rec:
            for workload in workloads:
                workload()
        assert rec._frames.live_count == 0, "frames left alive: an exit path is missing"
    finally:
        sys.path.remove(str(EXAMPLES))


def test_random_nesting_leaves_the_registry_empty() -> None:
    """Fuzz-ish: arbitrary try/except/generator nesting must still balance."""
    import random

    rng = random.Random(1234)  # noqa: S311  -- reproducibility, not security

    def maybe_raise(depth: int) -> Any:
        if depth <= 0:
            if rng.random() < 0.5:
                raise ValueError("deep")
            return 1
        try:
            if rng.random() < 0.4:
                return sum(g(depth))
            return maybe_raise(depth - 1)
        except ValueError:
            return 0

    def g(depth: int) -> Any:
        for i in range(2):
            yield maybe_raise(depth - 1) + i

    sink = MemorySink()
    rec = Recorder(sink)
    with rec:
        for _ in range(30):
            maybe_raise(3)

    assert rec._frames.live_count == 0, "registry not empty after fuzz"
    assert_frame_lifecycles_are_well_formed(sink.events)
