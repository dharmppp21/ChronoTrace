"""Shared recording helpers for the recorder tests.

Both `test_exceptions.py` and `test_generators.py` need the same two things: a
scope that admits exactly one example file, and a way to record one function from
it. They each grew their own copy; this is the one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from chronotrace.recorder import Event, MemorySink, Recorder
from chronotrace.recorder.scope import Scope

EXAMPLES = Path(__file__).parent.parent.parent / "examples"


class OnlyThisFile(Scope):
    """Inverts `Scope`: admit one file, exclude everything else.

    Day 5 learned the hard way that the recorder correctly records the *test
    harness* too -- those are real lines of real user code. Scoping to the example
    keeps the assertions about the example.

    Day 9 ships include-lists for users. This is the test-only shape of the same
    idea, built on the injection seam day 5 left open, which is what that seam was
    for.
    """

    def __init__(self, wanted: str) -> None:
        super().__init__()
        self._wanted = wanted

    def allows(self, filename: str) -> bool:
        return filename == self._wanted


def record_example(module_name: str, func_name: str) -> tuple[list[Event], Recorder]:
    """Record one function from an examples/ module, scoped to that file.

    Args:
        module_name: module under examples/, e.g. "generators".
        func_name: the function to call.

    Returns:
        The events, and the recorder (whose intern tables resolve code and
        exception ids).

    Complexity: O(events).
    """
    sys.path.insert(0, str(EXAMPLES))
    try:
        module: Any = __import__(module_name)
        sink = MemorySink()
        rec = Recorder(sink, scope=OnlyThisFile(module.__file__))
        with rec:
            getattr(module, func_name)()
        return sink.events, rec
    finally:
        sys.path.remove(str(EXAMPLES))
