"""Ground truth: what the program's state *actually* was, observed independently.

This file is the reason the harness proves anything. Everything else in ChronoTrace is
checked against ChronoTrace -- the day-20 oracle proves the fast reconstruction equals
the slow one, but if the **recorder** misunderstood the program, both are confidently
wrong together and every test stays green. Only an observer that shares no code with the
recorder can notice.

The shared-bug argument, stated plainly
---------------------------------------
A truth source built from the recorder's own machinery would inherit the recorder's
mistakes. If `FrameRegistry` fuses two frames, a truth source that asks `FrameRegistry`
which frame is current agrees with the fusion. If the seq counter double-counts, a truth
source reading that counter agrees. The test would then assert `X == X` and pass forever
while shipping a debugger that lies. **Independence is not a nicety here; it is the
entire value of the harness.**

So this observer is a second `sys.monitoring` tool, registered under its own tool id,
with its own callback, reading `frame.f_locals` directly. It never imports
`FrameRegistry`, the seq counter, `Event`, `ValuePool`, the dedup cache, or any part of
the recorder's capture-of-locals path.

What it *does* share, and why that is honest
--------------------------------------------
Three pure predicates: `capture` (how an object becomes bounded plain data), `Redactor`
(which names are secrets), `Scope` (which files count). These are shared deliberately:

* **`capture`** -- comparing a bounded representation against a raw object is not a
  comparison at all. Both sides must speak the same representation or every long list
  reads as a mismatch. The cost is stated in the README: a bug *inside* `capture` is
  invisible to this harness. It is covered by `tests/recorder/test_capture.py`, which
  checks the capture zoo against hand-written expectations rather than against itself.
* **`Redactor` / `Scope`** -- name and filename predicates, separately tested, and
  sharing them is what makes the two observers see the same variables in the same files
  by construction. A second implementation here would test our ability to write globs
  twice, not the recorder.

Everything that can actually be wrong -- which frame is live, which instant is which,
what changed, what was deduplicated, what was encoded, what was reconstructed -- is
observed independently.

No tearing, by construction
---------------------------
The observation and the recorder's capture of the same instant are separated by **no user
code**: two callbacks fire for one `LINE` event, and `capture` provably never invokes the
program's own code (day 7). So "the state at instant S" is unambiguous, and the day-7
tearing question does not arise. That is a property of observing live, and it is why the
harness is built this way instead of replaying the program a second time -- a second run
could diverge on anything non-deterministic and would compare two different executions.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, TracebackType
from typing import Any

from chronotrace.recorder.capture import DEFAULT_POLICY, CapturedValue, CapturePolicy, capture
from chronotrace.recorder.redact import REDACTED, Redactor
from chronotrace.recorder.scope import Scope

TOOL_ID = 5
"""The observer's `sys.monitoring` tool id. Deliberately the top of the range: the
recorder takes the lowest free id, so the two never contend for one."""

_HARNESS = str(Path(__file__).parent)
"""The observer must never observe itself. Its own `__enter__` runs *before* the recorder
starts, so an observation of it has no matching recorder event and derails the alignment
check with a confusing accusation. Excluded structurally rather than left to every caller
to remember when choosing a scope."""


@dataclass(frozen=True, slots=True)
class Observation:
    """The real state at one instant, seen without the recorder's help.

    Attributes:
        mark: `len(sink.events)` when the observation was taken. The observer's callback
            runs before the recorder's for the same event, so this is the `seq` the
            recorder is about to give that `LINE` -- the label that ties an observation
            to an instant. The driver verifies that claim on every observation rather
            than trusting it; see `equivalence.__init__`.
        code: the code object executing.
        lineno: the line about to execute.
        bindings: `name -> captured value`, the frame's real locals under the same
            capture policy and redaction rule the recorder uses.
        stack: `(code, lineno)` for every in-scope frame on the real call stack,
            innermost first. Suspended generators are absent -- they are on no stack,
            which is the day-6 claim the comparison checks.
    """

    mark: int
    code: CodeType
    lineno: int
    bindings: dict[str, CapturedValue]
    stack: tuple[tuple[CodeType, int], ...]


class TruthObserver:
    """A second `sys.monitoring` tool that snapshots real frame state at every line.

    Use as a context manager, *around* the recorder's own context so it is installed
    first. Costs a full capture of every local on every line -- far more than the
    recorder pays, which is why sampling exists.

    Raises:
        ValueError: `TOOL_ID` is already in use (`sys.monitoring` allows six tools).
    """

    def __init__(
        self,
        events: list[Any],
        scope: Scope,
        *,
        policy: CapturePolicy = DEFAULT_POLICY,
        redact: Redactor | None = None,
    ) -> None:
        """Build an observer.

        Args:
            events: the recorder's sink list, read only for its **length** -- never its
                contents. That length is the instant label and the one piece of the
                recorder this file touches; see `Observation.mark`.
            scope: which files to observe. Must be the recorder's, so both see the same
                lines (module docstring).
            policy: the capture policy, which must be the recorder's for the same reason.
            redact: the redaction rule, likewise.
        """
        self.observations: list[Observation] = []
        self._events = events
        self._scope = scope
        self._policy = policy
        self._redact = redact if redact is not None else Redactor()

    def _on_line(self, code: CodeType, lineno: int) -> Any:
        if code.co_filename.startswith(_HARNESS) or not self._scope.allows(code.co_filename):
            return sys.monitoring.DISABLE
        frame = sys._getframe(1)
        self.observations.append(
            Observation(
                mark=len(self._events),
                code=code,
                lineno=lineno,
                bindings=self._snapshot(frame.f_locals),
                stack=self._walk(frame),
            )
        )
        return None

    def _snapshot(self, live_locals: dict[str, Any]) -> dict[str, CapturedValue]:
        """Every local, captured under the shared policy, secrets withheld unread."""
        return {
            name: REDACTED if self._redact.should_redact(name) else capture(value, self._policy)
            for name, value in live_locals.items()
        }

    def _walk(self, frame: Any) -> tuple[tuple[CodeType, int], ...]:
        """The in-scope call stack, innermost first, straight off `f_back`.

        Out-of-scope frames (pytest, the harness itself) are skipped rather than ending
        the walk: a recorded frame can be called *through* untraced code, and stopping at
        the first stranger would hide the frames above it.
        """
        stack = []
        while frame is not None:
            if self._scope.allows(frame.f_code.co_filename):
                stack.append((frame.f_code, frame.f_lineno))
            frame = frame.f_back
        return tuple(stack)

    def __enter__(self) -> TruthObserver:
        mon = sys.monitoring
        mon.use_tool_id(TOOL_ID, "chronotrace-truth")
        mon.register_callback(TOOL_ID, mon.events.LINE, self._on_line)
        mon.set_events(TOOL_ID, mon.events.LINE)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        mon = sys.monitoring
        mon.set_events(TOOL_ID, 0)
        mon.register_callback(TOOL_ID, mon.events.LINE, None)
        mon.free_tool_id(TOOL_ID)
