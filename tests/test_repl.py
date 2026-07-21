"""The REPL is throwaway, but it is the only thing a human touches today.

So it gets the tests that matter for a command line: every command moves where it claims,
boundaries print instead of raising, and a typo does not end the session. The input loop
itself is untested on purpose -- `do()` is the seam, and `input()` is not our code.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.repl import Repl
from chronotrace.store import ChronoReader, ChronoWriter

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture(scope="module")
def session_parts() -> tuple[bytes, dict[int, str], dict[int, str]]:
    """Record `examples/simple.py` once, as `chronotrace step` does."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        simple: Any = __import__("simple")
        sink = MemorySink()
        recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(EXAMPLES)]))
        with recorder:
            simple.main()
    finally:
        sys.path.remove(str(EXAMPLES))
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=16, keyframe_interval=8)
    for captured in recorder.values:
        writer.add_value(captured)
    for event in sink.events:
        writer.add(event)
    writer.close()
    names = dict(enumerate(recorder.names))
    codes = {i: c.co_qualname for i, c in enumerate(recorder.codes)}
    return buf.getvalue(), names, codes


@pytest.fixture
def repl(session_parts: tuple[bytes, dict[int, str], dict[int, str]]) -> Repl:
    data, names, codes = session_parts
    return Repl(ChronoReader.from_bytes(data), names=names, codes=codes)


def test_it_starts_at_the_beginning(repl: Repl) -> None:
    assert repl.seq == 0


def test_n_and_p_are_inverses(repl: Repl) -> None:
    """The symmetry property, at the level a user experiences it."""
    repl.do("g 20")
    repl.do("n")
    forward = repl.seq
    repl.do("p")
    assert repl.seq == 20
    assert forward > 20


def test_step_over_back_skips_the_call(repl: Repl) -> None:
    """`O` from seq 26 lands on 4 -- the twenty-one-event skip, through the REPL."""
    repl.do("g 26")
    repl.do("O")
    assert repl.seq == 4


def test_F_returns_to_the_call_that_entered_the_frame(repl: Repl) -> None:
    repl.do("g 20")
    repl.do("F")
    assert repl.seq == 17


def test_a_boundary_prints_and_does_not_move(
    repl: Repl, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running off the start is an ordinary answer, not a traceback."""
    repl.do("g 1")
    capsys.readouterr()
    repl.do("p")
    assert "beginning of the recording" in capsys.readouterr().out
    assert repl.seq == 1


def test_p_with_an_argument_prints_a_variable(
    repl: Repl, capsys: pytest.CaptureFixture[str]
) -> None:
    """seq 11 binds `n` inside `double`; `p n` must show its value at that instant."""
    repl.do("g 11")
    capsys.readouterr()
    repl.do("p n")
    assert "n = 0" in capsys.readouterr().out


def test_a_variable_not_yet_assigned_says_so(
    repl: Repl, capsys: pytest.CaptureFixture[str]
) -> None:
    """Time travel's whole point: at seq 11, `result` has not been computed yet.

    It must say "not bound", never print `None` -- a debugger that invents a value for a
    name the program had not reached is worse than one that refuses.
    """
    repl.do("g 11")
    capsys.readouterr()
    repl.do("p result")
    out = capsys.readouterr().out
    assert "not bound" in out
    assert "None" not in out


def test_bt_shows_the_stack_innermost_first(repl: Repl, capsys: pytest.CaptureFixture[str]) -> None:
    repl.do("g 11")
    capsys.readouterr()
    repl.do("bt")
    frames = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert frames[0].startswith("*"), "the current frame is starred and listed first"
    assert "double" in frames[0]
    assert "quadruple" in frames[1]
    assert "main" in frames[2]


def test_an_unknown_command_keeps_the_session_alive(
    repl: Repl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert repl.do("wat") is True
    assert "unknown command" in capsys.readouterr().out


def test_g_rejects_an_instant_outside_the_recording(
    repl: Repl, capsys: pytest.CaptureFixture[str]
) -> None:
    repl.do("g 999999")
    assert repl.seq == 0
    assert "needs an instant" in capsys.readouterr().out


def test_q_ends_the_session(repl: Repl) -> None:
    assert repl.do("q") is False
