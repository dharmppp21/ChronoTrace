"""Attaches to a running program and turns it into an event stream.

The recorder wires `sys.monitoring` (PEP 669) callbacks to the day-4 event model.
It is the only code in the project that runs *inside the user's hot path*: every
microsecond here is paid once per line of their program.

The invariant that outranks everything: **a callback must never raise**
--------------------------------------------------------------------
Not a style rule. Measured, on Python 3.14:

* A callback that raises **once** injects that exception into the target at
  whatever line was executing. It surfaces at a line the target never wrote, and
  the target's own `except BaseException` **does not necessarily catch it** --
  the injection point can be outside the handler that was about to protect it.
* A callback that raises **every time** -- the realistic case, because a broken
  sink is broken for every event -- takes CPython down its fatal
  `_PyObject_Dump` path: `lost sys.stderr`, exit code 1, no traceback. The
  exception handling itself executes lines, which re-fire the callback, which
  raises again.

So the user's program is not merely disturbed; it becomes unrecoverable. Every
callback body is therefore wrapped, and failures degrade the *recording* rather
than the *program*. Their program's correctness outranks our recording, always.

The lifecycle contract
----------------------
`start()` acquires a `sys.monitoring` tool id; `stop()` releases it and is
idempotent. `stop()` must run on every path including exceptions, because a
leaked tool id is unrecoverable without restarting the interpreter: there are
only six ids, and the next `Recorder()` in the same process would find ours still
held and refuse to attach. `try/finally` is not optional; use the context manager.

Known fidelity gaps (dated, not forgotten)
------------------------------------------
* **C functions emit no LINE events.** `json.loads`, `re.match` and numpy run
  their real work in C, so a recording shows the call and nothing inside it. This
  is inherent to PEP 669's Python-level events, not a bug we can fix.
* **Generators break the stack model** (day 6). `PY_START` fires once, but the
  frame suspends at `YIELD` and re-enters at `RESUME` without a matching
  `PY_RETURN`. Today's stack silently mis-nests them. `test_recorder.py` carries a
  skipped test naming this; day 6 replaces the stack with a live-frame registry.
* **Exceptions unwind frames without `PY_RETURN`** (day 6), so the stack leaks a
  frame per uncaught exception. Same fix.
* **Values are not captured** (day 7). Today records control flow only, so that
  when something breaks it is obvious which half broke.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from types import CodeType, TracebackType
from typing import Any

from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.interning import InternTable
from chronotrace.recorder.scope import Scope
from chronotrace.recorder.sink import Sink

_TOOL_PREFERENCE = (sys.monitoring.DEBUGGER_ID, 3, 4)
"""Ids to try, in order.

PEP 669 defines six. ChronoTrace is a debugger, so it asks for `DEBUGGER_ID`
first, then falls back to the two general-purpose ids. It never takes an id
another tool holds: a debugger that evicts coverage.py mid-run has broken
somebody else's session to run its own.
"""

_NO_FRAME = 0
"""Frame id for events whose frame we never saw start.

Recording can begin mid-execution, so LINE events arrive for frames that were
already on the stack. They are real events and belong in the timeline; only their
parentage is unknown. Dropping them would lose real history to protect a bookkeeping
invariant.
"""


class _ThreadState(threading.local):
    """Per-thread frame stack.

    `sys.monitoring.set_events` is process-global: callbacks fire on every thread.
    One shared stack would interleave threads' frames into nonsense, so the stack
    is thread-local. `threading.local` costs an attribute lookup per event, which
    is the price of being correct under threads from day one rather than
    discovering it later.
    """

    def __init__(self) -> None:
        self.stack: list[int] = []


class Recorder:
    """Records a program's control flow into a `Sink`.

    Usage::

        with Recorder(MemorySink()) as rec:
            run_the_program()
        rec.sink.events  # what happened

    Attributes are read-only after construction. One recorder owns one tool id at
    a time; `start()` twice without `stop()` is an error, not a no-op, because it
    would silently mean two things believe they are recording.
    """

    __slots__ = (
        "_codes",
        "_dropped",
        "_frames",
        "_scope",
        "_seq",
        "_state",
        "_tool_id",
        "sink",
    )

    def __init__(self, sink: Sink, scope: Scope | None = None) -> None:
        """Build a recorder. Nothing is instrumented until `start()`.

        Args:
            sink: where events go. Must not raise from `emit`; if it does, the
                recorder degrades rather than propagating (see module docstring).
            scope: which code to record. Defaults to "everything except
                ChronoTrace itself". Injectable for tests; day 9 makes it
                user-configurable.
        """
        self.sink = sink
        self._scope = scope if scope is not None else Scope()
        self._codes: InternTable[CodeType] = InternTable()
        self._seq = itertools.count()
        self._frames = itertools.count(1)  # 0 is _NO_FRAME
        self._state = _ThreadState()
        self._tool_id: int | None = None
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """Events lost because the sink failed. Non-zero means the recording is incomplete."""
        return self._dropped

    def start(self) -> None:
        """Acquire a tool id and begin recording.

        Raises:
            RuntimeError: already started, or every `sys.monitoring` tool id is
                held by another tool (the error names the holders).

        Complexity: O(1).
        """
        if self._tool_id is not None:
            raise RuntimeError("Recorder already started")
        if sys.gettrace() is not None:
            # pdb and coverage's older mode use sys.settrace. Both mechanisms can
            # coexist, but the target pays for both, and a user under pdb almost
            # certainly did not mean to.
            print(  # noqa: T201
                "chronotrace: a sys.settrace tracer is already installed "
                "(pdb? coverage?). Recording anyway; the target will be slower.",
                file=sys.stderr,
            )
        self._tool_id = self._acquire_tool_id()
        mon = sys.monitoring
        mon.register_callback(self._tool_id, mon.events.LINE, self._on_line)
        mon.register_callback(self._tool_id, mon.events.PY_START, self._on_start)
        mon.register_callback(self._tool_id, mon.events.PY_RETURN, self._on_return)
        mon.set_events(
            self._tool_id,
            mon.events.LINE | mon.events.PY_START | mon.events.PY_RETURN,
        )

    def stop(self) -> None:
        """Stop recording, release the tool id, close the sink. Idempotent.

        Idempotent because it runs on both the normal and the exception path, and
        a double stop must not become a second failure on top of the first.

        Complexity: O(1).
        """
        if self._tool_id is None:
            return
        tool_id, self._tool_id = self._tool_id, None
        mon = sys.monitoring
        try:
            mon.set_events(tool_id, 0)
            for event in (mon.events.LINE, mon.events.PY_START, mon.events.PY_RETURN):
                mon.register_callback(tool_id, event, None)
        finally:
            # Nested finally: if de-registration fails we still must not leak the
            # id. Six exist; leaking one makes every later Recorder in this
            # process unable to attach, and nothing short of a restart fixes it.
            mon.free_tool_id(tool_id)
            self.sink.close()

    def __enter__(self) -> Recorder:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # -- callbacks: the hot path. Every line here runs per line of the target. --

    def _on_line(self, code: CodeType, line_number: int) -> Any:
        try:
            if not self._scope.allows(code.co_filename):
                return sys.monitoring.DISABLE
            stack = self._state.stack
            self._emit(EventKind.LINE, code, stack[-1] if stack else _NO_FRAME, line_number)
        except Exception:
            self._dropped += 1
        return None

    def _on_start(self, code: CodeType, instruction_offset: int) -> Any:
        try:
            if not self._scope.allows(code.co_filename):
                return sys.monitoring.DISABLE
            frame_id = next(self._frames)
            self._state.stack.append(frame_id)
            self._emit(EventKind.CALL, code, frame_id, code.co_firstlineno)
        except Exception:
            self._dropped += 1
        return None

    def _on_return(self, code: CodeType, instruction_offset: int, retval: object) -> Any:
        try:
            if not self._scope.allows(code.co_filename):
                return sys.monitoring.DISABLE
            stack = self._state.stack
            # Underflow is expected, not exceptional: recording can start
            # mid-execution, so frames return that we never saw start.
            frame_id = stack.pop() if stack else _NO_FRAME
            self._emit(EventKind.RETURN, code, frame_id, code.co_firstlineno)
        except Exception:
            self._dropped += 1
        return None

    def _emit(self, kind: EventKind, code: CodeType, frame_id: int, lineno: int) -> None:
        self.sink.emit(
            Event(
                seq=next(self._seq),
                kind=kind,
                timestamp_ns=time.perf_counter_ns(),
                thread_id=threading.get_ident(),
                frame_id=frame_id,
                code_id=self._codes.intern(code),
                lineno=lineno,
            )
        )

    def _acquire_tool_id(self) -> int:
        mon = sys.monitoring
        for tool_id in _TOOL_PREFERENCE:
            if mon.get_tool(tool_id) is None:
                mon.use_tool_id(tool_id, "chronotrace")
                return tool_id
        holders = {i: mon.get_tool(i) for i in range(6) if mon.get_tool(i)}
        raise RuntimeError(
            f"no free sys.monitoring tool id; all are held: {holders}. "
            "Stop the other tool (coverage.py? another debugger?) and retry."
        )
