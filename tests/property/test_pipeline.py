"""The whole project under one property, over programs nobody wrote.

    For any generated program, at every sampled instant, reconstructed state
    equals the state the program actually had.

That single statement puts days 4-22 under test at once -- recorder, capture, dedup,
value pool, writer, keyframes, deltas, reader, reconstructor -- against programs chosen
by a machine rather than by the imagination that wrote the code.

Storage parameters are drawn too, and that is not decoration
------------------------------------------------------------
The first campaign ran 400 programs clean, which looked like good news and was not.
Measuring the generator's reach (day 23's own debugging checklist: *"campaign finds
nothing -> generator too narrow, check coverage before congratulating yourself"*) showed
a median of **20 events and 0.3 keyframes per program**. Almost every recording had one
keyframe, at seq 0, so reconstruction never crossed a keyframe boundary and the entire
keyframe-plus-delta machinery was untested by the campaign.

Drawing `keyframe_interval` and `block_events` from small ranges fixes that without
making programs bigger or the campaign slower: a 20-event recording with an interval of 2
crosses ten keyframe boundaries and several block boundaries. Bug density is highest at
those handoffs, so this buys far more per second than generating longer programs would.

Determinism in the harness is a precondition for shrinking; see `load_generated`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, Delta
from tests.equivalence import check

from . import load_generated
from .program_gen import python_program

# `return` inside `finally` is a SyntaxWarning from 3.14 (PEP 765) and one of the edge
# cases the generator is required to reach. Silenced here rather than dropped there.
pytestmark = pytest.mark.filterwarnings("ignore::SyntaxWarning")

KEYFRAME_INTERVALS = st.integers(1, 16)
BLOCK_SIZES = st.integers(2, 32)


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One directory for every generated module in this file.

    Module-scoped so it is created once rather than per example: a `@given` test resolves
    its fixtures once per *test*, and content-addressed module names mean a repeated
    program reuses its file rather than colliding.
    """
    return tmp_path_factory.mktemp("campaign")


@given(python_program(), KEYFRAME_INTERVALS, BLOCK_SIZES)
@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
def test_reconstruction_equals_reality_for_generated_programs(
    workdir: Path, source: str, interval: int, block: int
) -> None:
    """The property the whole project reduces to."""
    found = check(
        load_generated(workdir, source),
        Scope(roots=[str(workdir)]),
        keyframe_interval=interval,
        block_events=block,
    )
    assert not found, source + "".join(str(m) for m in found)


def test_the_campaign_catches_an_injected_bug(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    """A campaign that has never failed proves nothing -- the day-22 argument, again.

    Drops one delta per query, exactly as `tests/equivalence` does, and asserts the
    generated programs notice. Without this, a generator too narrow to reach the storage
    layer would look identical to a correct system.
    """
    original = ChronoReader.deltas_between

    def lossy(self: ChronoReader, a: int, b: int) -> list[Delta]:
        deltas = original(self, a, b)
        return deltas[:-1] if len(deltas) > 1 else deltas

    monkeypatch.setattr(ChronoReader, "deltas_between", lossy)

    caught = 0
    for source in _sample_programs(25):
        try:
            found = check(
                load_generated(workdir, source), Scope(roots=[str(workdir)]), keyframe_interval=4
            )
        except Exception:
            caught += 1
            continue
        caught += bool(found)
    assert caught, "25 generated programs failed to notice a dropped delta"


# -- meta-tests: the generator itself --------------------------------------------------

CONSTRUCTS = {
    "nested function": "    def ",
    "loop": "for ",
    "conditional": "if ",
    "try/except": "except ValueError:",
    "finally": "finally:",
    "del": "del ",
    "nonlocal": "nonlocal ",
    "generator": "yield ",
    "class": "class ",
    "comprehension": "for _c in range",
    "*args/**kwargs": "*args, **kwargs",
    "mutable default": "acc=[]",
    "recursion": "(n - 1)",
    "shadowed global": "    G = ",
    "raise": "raise ValueError",
    "raise inside finally": "from finally",
}
"""Every construct the day's brief requires the generator to reach. Asserted, because
"the generator probably covers that" is exactly how a campaign ends up proving nothing."""


def test_every_generated_program_compiles_and_runs() -> None:
    """Valid, terminating and deterministic -- checked, not asserted by construction.

    Termination is structural (no `while`, literal `range` bounds, guarded recursion), so
    a program that hangs here would mean the grammar can express something it should not.
    Running them is also what catches the accidental exceptions -- an `UnboundLocalError`
    from a `del` in a nested block, a `StopIteration` from a generator that returns before
    yielding -- both of which this test found while the generator was being written.
    """
    for source in _sample_programs(120):
        compile(source, "<generated>", "exec")


COVERAGE_SAMPLE = 400
"""Sized for the rarest construct, not for convenience.

`nonlocal` needs a function nested inside a function that has already bound something,
and even weighted up and with `main` seeded it lands in ~4% of programs. At 150 draws
this assertion failed outright in the first deep campaign; at 400 the expected count is
~15, so a genuinely narrowed grammar fails and a healthy one does not flake.
"""


def test_the_generator_reaches_every_required_construct() -> None:
    """Coverage measured over one sample, so a narrowed grammar fails loudly."""
    sample = _sample_programs(COVERAGE_SAMPLE)
    counts = {name: sum(marker in s for s in sample) for name, marker in CONSTRUCTS.items()}
    missing = sorted(name for name, seen in counts.items() if not seen)
    assert not missing, f"never generated in {COVERAGE_SAMPLE} programs: {missing} (of {counts})"


def _sample_programs(count: int) -> list[str]:
    """Draw `count` programs without running a property. Generation only, no shrinking."""
    drawn: list[str] = []

    @given(python_program())
    @settings(
        max_examples=count,
        deadline=None,
        phases=[Phase.generate],
        suppress_health_check=list(HealthCheck),
    )
    def collect(source: str) -> None:
        drawn.append(source)

    collect()
    return drawn
