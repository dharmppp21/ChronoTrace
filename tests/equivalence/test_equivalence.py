"""The referee: reconstructed state must equal the state the program actually had.

Every other test in this project checks ChronoTrace against ChronoTrace. This one checks
it against reality, observed live by an independent mechanism (`truth.py`). It is the
only test that can catch the recorder and the reconstructor being wrong *together*.

**A test suite that has never been proven to fail is not evidence of anything.** So half
this file deliberately breaks the system -- a dropped delta, a lying keyframe, a
content-blind dedup cache, a drifting reconstruction cache -- and asserts the referee goes
red for each. Without those, "green" would mean only that the comparator is lenient.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from chronotrace.reconstruct import KeyframeReconstructor
from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.recorder.values import ValuePool
from chronotrace.store import ChronoReader
from chronotrace.store.writer import ChronoWriter
from tests.fixtures import hostile, programs

from . import Mismatch, check, record
from .minimise import harness_oracle, minimise
from .truth import TruthObserver

EXAMPLES = Path(__file__).parent.parent.parent / "examples"
FIXTURES = Path(__file__).parent.parent / "fixtures"
EXAMPLE_SCOPE = Scope(roots=[str(EXAMPLES)])
FIXTURE_SCOPE = Scope(roots=[str(FIXTURES)])

PROGRAMS = ["simple", "generators", "exceptions", "buggy_pipeline", "pipeline_realistic"]

KNOWN_DIVERGENCES: dict[str, list[tuple[str, str]]] = {
    # Issue #7, found by this harness on its first run: `del x` leaves no trace in
    # `f_locals`, so the recorder never emits anything and reconstruction carries the
    # dead binding forward forever. Listed as an exact expectation rather than forgiven
    # in the comparator -- when the recorder learns about deletion this assertion fails
    # and the entry gets deleted, which is the point.
    "generators": [("extra", "gen")],
}


def _load(module: str) -> Any:
    sys.path.insert(0, str(EXAMPLES))
    try:
        return __import__(module)
    finally:
        sys.path.remove(str(EXAMPLES))


# -- the referee ------------------------------------------------------------------------


@pytest.mark.parametrize("program", PROGRAMS)
def test_reconstruction_equals_reality(program: str) -> None:
    """Every example, every instant: what we replay is what actually happened."""
    found = check(_load(program).main, EXAMPLE_SCOPE)
    actual = sorted((m.kind, m.name) for m in found)
    expected = sorted(KNOWN_DIVERGENCES.get(program, []))
    assert actual == expected, "".join(str(m) for m in found)


def test_hostile_objects_reconstruct_faithfully() -> None:
    """The capture zoo: cycles, 10M-element lists, liars, objects that explode on repr.

    Policy makes these lossy, and the observer applies the same policy -- so a truncated
    list must match a *truncated* list exactly. Nothing here is forgiven for being big.
    """
    hostile.reset_sentinels()
    zoo = hostile.build_zoo()
    found = check(lambda: programs.holds_the_zoo(zoo), FIXTURE_SCOPE)
    assert found == [], "".join(str(m) for m in found)


def test_a_redacted_secret_is_withheld_on_both_sides() -> None:
    """A secret must be marked, not missing, and must match the marker -- never the value."""
    assert check(programs.holds_a_secret, FIXTURE_SCOPE) == []
    recording = record(programs.holds_a_secret, FIXTURE_SCOPE)
    seen = {
        name
        for _seq, observation in recording.instants
        for name, value in observation.bindings.items()
        if value == {"$": "redacted"}
    }
    assert "password" in seen, "the harness must exercise redaction, not skip it"


# -- proof the referee can fail ----------------------------------------------------------


def test_catches_a_dropped_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconstruction loses one change. The state stays plausible -- and wrong."""
    original = ChronoReader.deltas_between

    def lossy(self: ChronoReader, a: int, b: int) -> list[Any]:
        deltas = original(self, a, b)
        return deltas[:-1] if len(deltas) > 1 else deltas

    monkeypatch.setattr(ChronoReader, "deltas_between", lossy)
    assert check(_load("simple").main, EXAMPLE_SCOPE), "a dropped delta went unnoticed"


def test_catches_a_keyframe_that_under_reports_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyframe claiming a frame holds nothing -- what a writer bug would produce.

    Injected at the read boundary rather than inside `LiveState`, because mutating the
    writer's live state would corrupt the delta stream too and prove less: this isolates
    the keyframe as the liar.
    """
    original = ChronoReader.nearest_keyframe_at_or_before

    def under_reporting(self: ChronoReader, seq: int) -> Any:
        keyframe = original(self, seq)
        if keyframe is None or not keyframe.frames:
            return keyframe
        first = replace(keyframe.frames[0], local_refs={})
        return replace(keyframe, frames=[first, *keyframe.frames[1:]])

    monkeypatch.setattr(ChronoReader, "nearest_keyframe_at_or_before", under_reporting)
    # A short interval on purpose: at the default, a 53-event program has exactly one
    # keyframe -- at seq 0, where the live state is empty and corrupting it changes
    # nothing. The injection has to land on a keyframe that actually carries state.
    found = check(_load("simple").main, EXAMPLE_SCOPE, keyframe_interval=8)
    assert found, "a lying keyframe went unnoticed"


def test_a_merely_absent_keyframe_is_correctly_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not an injected bug -- a claim being verified end to end.

    Day 15 designed keyframes so a lost one costs latency, never correctness:
    reconstruction falls back to the previous one and replays further. Dropping half of
    them must therefore leave the referee green. If this ever goes red, graceful
    degradation is a story rather than a property.
    """
    original = ChronoWriter._emit_keyframe
    calls = iter(range(1_000_000))

    def every_other(self: ChronoWriter, seq: int) -> None:
        if next(calls) % 2 == 0:
            original(self, seq)

    monkeypatch.setattr(ChronoWriter, "_emit_keyframe", every_other)
    found = check(_load("simple").main, EXAMPLE_SCOPE)
    assert found == [], "".join(str(m) for m in found)


def test_catches_content_blind_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The day-8 bug: dedup that cannot see a mutable change underneath it.

    Here the cache keys a list on its *length*, so `data[0] = 2` returns the reference
    from before the mutation. That is precisely why `_capture_locals` re-captures every
    local every line instead of taking an identity shortcut.
    """
    original = ValuePool.add

    def blind(self: ValuePool, captured: Any) -> Any:
        if isinstance(captured, dict) and captured.get("$") == "list":
            captured = {"$": "list", "len": captured.get("len"), "items": []}
        return original(self, captured)

    monkeypatch.setattr(ValuePool, "add", blind)
    found = check(programs.mutates_in_place, FIXTURE_SCOPE)
    assert found, "a stale reference from content-blind dedup went unnoticed"


def test_catches_a_drifting_reconstruction_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0006 section 4's named nightmare: a cached state that is plausible but off by one.

    The one failure a debugger cannot survive, because nothing about the answer looks
    wrong. If the referee could not catch this, the cache would be unfalsifiable.
    """
    original = KeyframeReconstructor.reconstruct

    def drifting(self: KeyframeReconstructor, seq: int) -> Any:
        return original(self, seq - 1 if seq > 0 else seq)

    monkeypatch.setattr(KeyframeReconstructor, "reconstruct", drifting)
    assert check(_load("simple").main, EXAMPLE_SCOPE), "a drifting cache went unnoticed"


# -- the harness's own assumptions -------------------------------------------------------


def test_the_observer_does_not_perturb_the_recording() -> None:
    """Two `sys.monitoring` tools at once must not change what the recorder sees.

    If observing changed the recording, the harness would validate a program that only
    exists while being watched. Timestamps are excluded; everything identifying an event
    is compared.
    """
    simple = _load("simple")

    def run(observed: bool) -> list[tuple[Any, ...]]:
        sink = MemorySink()
        recorder = Recorder(sink, scope=EXAMPLE_SCOPE, capture_values=True)
        if observed:
            with TruthObserver(sink.events, EXAMPLE_SCOPE), recorder:
                simple.main()
        else:
            with recorder:
                simple.main()
        return [(e.seq, e.kind, e.frame_id, e.code_id, e.lineno, e.name_id) for e in sink.events]

    assert run(False) == run(True)


def test_sampling_keeps_every_boundary() -> None:
    """A budget shrinks the middle, never the handoffs. See the package docstring."""
    recording = record(_load("generators").main, EXAMPLE_SCOPE)
    full = check(_load("generators").main, EXAMPLE_SCOPE)
    sampled = check(_load("generators").main, EXAMPLE_SCOPE, limit=12)
    assert len(recording.instants) > 12, "the fixture must be big enough to force sampling"
    # The `del` divergence sits at a frame boundary, so a budget must not lose it.
    assert {(m.kind, m.name) for m in sampled} == {(m.kind, m.name) for m in full}


def test_minimisation_shrinks_a_real_failure(tmp_path: Path) -> None:
    """The `del` divergence, reduced from a padded program to a handful of lines.

    Uses a genuine failure rather than a synthetic one: minimisation that only works on
    a bug you invented for it is not a debugging tool.
    """
    source = (
        "def helper(n):\n"
        "    total = 0\n"
        "    for i in range(n):\n"
        "        total += i\n"
        "    return total\n"
        "\n"
        "def main():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = helper(3)\n"
        "    doomed = [1, 2, 3]\n"
        "    del doomed\n"
        "    d = a + b\n"
        "    return c + d\n"
    )
    oracle = harness_oracle(tmp_path, Mismatch(0, "extra", "", "doomed", None, None))
    reduced = minimise(source, oracle)
    lines = [line for line in reduced.splitlines() if line.strip()]
    assert len(lines) < 20, reduced
    assert "del doomed" in reduced, reduced
    assert "helper" not in reduced, f"unrelated code survived minimisation:\n{reduced}"


def test_minimisation_refuses_a_failure_it_cannot_reproduce() -> None:
    """Shrinking towards a failure that never happens would return confident noise."""
    with pytest.raises(ValueError, match="does not reproduce"):
        minimise("x = 1\n", lambda _source: False)
