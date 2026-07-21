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
* **Threads share one `seq` clock but not one stack.** The frame registry is
  per-thread for execution order and process-wide for identity; a generator
  created on one thread and resumed on another keeps its id. Untested under real
  contention beyond `test_events.py`'s seq test -- day 10 exercises it.
* **Abandoned generators leak one change-detection entry.** A generator that
  suspends and is never resumed to completion keeps its `_last_ref` slot until the
  process ends -- bounded by the number of such generators, the same shape of gap
  as the frame registry's own.

Closed on day 8: locals are captured (day 7) and deduplicated on content, and a
VAR_WRITE fires only when a binding's value actually changed. `capture_values` is
still a flag so days 40-41 can profile control flow and values apart.

Closed on day 6: generators and coroutines now keep one `frame_id` across their
whole life (see `frames.py`), exceptions unwind without leaking frames, and
`RAISE` marks origins only.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from types import CodeType, TracebackType
from typing import Any

from chronotrace.recorder.capture import DEFAULT_POLICY, CapturePolicy, capture
from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.frames import FrameRegistry
from chronotrace.recorder.identity import ObjectIdentity
from chronotrace.recorder.interning import InternTable
from chronotrace.recorder.redact import REDACTED, Redactor
from chronotrace.recorder.scope import Scope
from chronotrace.recorder.sink import Sink
from chronotrace.recorder.values import ValuePool, ValueRef

# Events that reject `DISABLE`. Measured, because the failure is silent.
#
# Returning `sys.monitoring.DISABLE` from these raises
# ``ValueError: Cannot disable X events. Callback removed.`` -- and the message is
# not a warning, it is a description: **CPython unregisters the callback**. A
# recorder that did this would stop recording exceptions and never say so.
#
# `LINE`, `PY_START`, `PY_RESUME`, `PY_YIELD`, `PY_RETURN` and `RERAISE` all accept
# it. The three that do not are exactly the exception events, which makes sense:
# `DISABLE` de-instruments a *code location*, and an exception is not a location --
# it can arrive at any instruction.
#
# The consequence for day 9's scope filter is real and worth stating: scoping makes
# out-of-scope code stop calling us for LINE/CALL/RETURN, but exception events keep
# firing for the entire process forever. The out-of-scope exception callbacks
# therefore return `None` and emit nothing -- we pay the call and drop the result.
# Exceptions are rare next to lines, so the cost is small, but it is not zero and it
# cannot be optimised away.

_TOOL_PREFERENCE = (sys.monitoring.DEBUGGER_ID, 3, 4)
"""Ids to try, in order.

PEP 669 defines six. ChronoTrace is a debugger, so it asks for `DEBUGGER_ID`
first, then falls back to the two general-purpose ids. It never takes an id
another tool holds: a debugger that evicts coverage.py mid-run has broken
somebody else's session to run its own.
"""


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
        "_capture_values",
        "_codes",
        "_dropped",
        "_exc_types",
        "_frames",
        "_identity",
        "_last_ref",
        "_names",
        "_policy",
        "_propagating",
        "_redact",
        "_scope",
        "_seq",
        "_tool_id",
        "_values",
        "sink",
    )

    def __init__(
        self,
        sink: Sink,
        scope: Scope | None = None,
        *,
        capture_values: bool = True,
        policy: CapturePolicy = DEFAULT_POLICY,
        redact: Redactor | None = None,
    ) -> None:
        """Build a recorder. Nothing is instrumented until `start()`.

        Args:
            sink: where events go. Must not raise from `emit`; if it does, the
                recorder degrades rather than propagating (see module docstring).
            scope: which code to record. Defaults to the current working
                directory's tree, excluding the stdlib, site-packages and
                ChronoTrace itself. The config layer builds this from user flags.
            capture_values: record local variable values, not just control flow.
                A flag rather than a hard-coded truth because day 3 measured value
                capture as the dominant cost (2,370x naive, 6.1x with change
                detection), and days 40-41 need to profile the two halves apart.
                Turning it off yields day 5's recorder exactly.
            policy: what one captured value may cost. See `capture.CapturePolicy`.
            redact: decides which locals are secrets to withhold. Defaults to the
                standard name patterns -- redaction is on by default because
                failing safe means never leaking a token no one remembered to
                mask. The recorder takes a `Redactor`, not a config object, so it
                stays the bottom layer and never imports the config system.
        """
        self.sink = sink
        self._scope = scope if scope is not None else Scope()
        self._capture_values = capture_values
        self._policy = policy
        self._redact = redact if redact is not None else Redactor()
        self._identity = ObjectIdentity()
        self._names: InternTable[str] = InternTable()
        self._values = ValuePool()
        self._codes: InternTable[CodeType] = InternTable()
        self._exc_types: InternTable[str] = InternTable()
        self._seq = itertools.count()
        self._frames = FrameRegistry()
        self._tool_id: int | None = None
        self._dropped = 0
        # Per-frame change-detection: frame_id -> {name_id -> last ValueRef}.
        # A VAR_WRITE fires only when a binding's ref actually changed, so the
        # stream carries deltas, not a full re-statement of every local on every
        # line. Dropped whole-frame on RETURN/UNWIND so it stays bounded by live
        # frames rather than growing one entry per (frame, local) forever.
        self._last_ref: dict[int, dict[int, ValueRef]] = {}
        # id() of the exception currently propagating, so that RAISE fires only
        # where an exception is BORN. CPython re-fires RAISE in every frame it
        # crosses; see EventKind.RAISE. Safe to key on id() because the object is
        # alive for the whole propagation -- the frames it is unwinding hold it.
        self._propagating: int | None = None

    @property
    def dropped(self) -> int:
        """Events lost because the sink failed. Non-zero means the recording is incomplete."""
        return self._dropped

    @property
    def names(self) -> InternTable[str]:
        """`name_id` -> variable name. Read-only; the recorder keeps interning while it runs.

        Every layer above receives ids, so something has to map them back, and until the
        `.chrono` format persists these tables (issue #6) the live recorder is the only
        authority. That is also why `store` cannot own them.
        """
        return self._names

    @property
    def codes(self) -> InternTable[CodeType]:
        """`code_id` -> the code object it names, for filename/qualname/first line."""
        return self._codes

    @property
    def values(self) -> ValuePool:
        """`value_ref` -> the captured representation.

        Iterable in reference order, which is what a writer needs to persist the pool
        under the references the events already cite.
        """
        return self._values

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
        for event, callback in self._callbacks():
            mon.register_callback(self._tool_id, event, callback)
        mon.set_events(self._tool_id, self._event_mask())

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
            for event, _ in self._callbacks():
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
            frame_id = self._frames.current
            self._emit(EventKind.LINE, code, frame_id, line_number)
            if self._capture_values:
                self._capture_locals(code, frame_id, line_number)
        except Exception:
            self._dropped += 1
        return None

    def _capture_locals(self, code: CodeType, frame_id: int, line_number: int) -> None:
        """Emit a VAR_WRITE only for locals whose value actually changed.

        This is the delta-encoding of Phase 2 starting early, and it must live
        here rather than in the store: the store sees only Events and cannot know
        a local *didn't* change without being told, while the recorder is the only
        layer that ever sees `f_locals` and can compare. So the recorder omits the
        non-change; reconstruct later carries the last VAR_WRITE forward.

        Every local is re-captured every line -- no identity shortcut, because a
        mutable object mutated in place keeps its `id()` (see dedup.py). Capture is
        bounded, dedup collapses the repeats to one reference, and an unchanged
        reference emits nothing. Day 3 measured the naive always-emit version at
        **2,370x** on json_pipeline; this is the fix.

        Reconciliation runs both ways: names whose value changed are emitted, and names
        that have *gone* are emitted as a valueless VAR_WRITE. Tracking the live name ids
        to see the second costs one set per line -- measured at +3.7% of recording time,
        against `capture()`, which dominates it.
        """
        frame = sys._getframe(2)
        last = self._last_ref.get(frame_id)
        if last is None:
            last = self._last_ref[frame_id] = {}
        live = frame.f_locals
        present: set[int] = set()
        for name, value in live.items():
            name_id = self._names.intern(name)
            present.add(name_id)
            # Redaction is a *read* gate: a secret-named local never reaches
            # capture(), so its value is never copied into our buffers. The name
            # is still recorded; only the value becomes a marker.
            if self._redact.should_redact(name):
                captured = REDACTED
            else:
                captured = capture(value, self._policy, self._identity)
            ref = self._values.add(captured)
            if last.get(name_id) == ref:
                continue  # unchanged since we last saw this binding: record nothing
            last[name_id] = ref
            self._emit(
                EventKind.VAR_WRITE, code, frame_id, line_number, name_id=name_id, value_ref=ref
            )
        # A binding that has *vanished* since the last line was deleted (`del x`), and a
        # deleted name simply leaves `f_locals` -- so nothing notices unless we look for
        # the absence. Without this the debugger keeps showing `x` for the rest of the
        # frame, which is a lie about the program rather than a lossy view of it (#7).
        # The materialised list is required: the loop mutates `last`.
        for name_id in [n for n in last if n not in present]:
            del last[name_id]
            self._emit(EventKind.VAR_WRITE, code, frame_id, line_number, name_id=name_id)

    def _on_start(self, code: CodeType, instruction_offset: int) -> Any:
        try:
            if not self._scope.allows(code.co_filename):
                return sys.monitoring.DISABLE
            frame_id = self._frames.enter(sys._getframe(1))
            self._emit(EventKind.CALL, code, frame_id, code.co_firstlineno)
        except Exception:
            self._dropped += 1
        return None

    def _on_resume(self, code: CodeType, instruction_offset: int) -> Any:
        """A generator or coroutine re-entered.

        `enter` recovers the *same* frame_id assigned at PY_START. That recovery is
        the entire reason frames.py exists.
        """
        try:
            if not self._scope.allows(code.co_filename):
                return sys.monitoring.DISABLE
            frame_id = self._frames.enter(sys._getframe(1), resuming=True)
            self._emit(EventKind.RESUME, code, frame_id, code.co_firstlineno)
        except Exception:
            self._dropped += 1
        return None

    def _on_yield(self, code: CodeType, instruction_offset: int, retval: object) -> Any:
        """A generator suspended: it stops executing but stays alive."""
        try:
            if not self._scope.allows(code.co_filename):
                return sys.monitoring.DISABLE
            frame_id = self._frames.suspend(sys._getframe(1))
            self._emit(EventKind.YIELD, code, frame_id, code.co_firstlineno)
        except Exception:
            self._dropped += 1
        return None

    def _on_return(self, code: CodeType, instruction_offset: int, retval: object) -> Any:
        try:
            if not self._scope.allows(code.co_filename):
                return sys.monitoring.DISABLE
            frame_id = self._frames.exit(sys._getframe(1))
            self._last_ref.pop(frame_id, None)  # frame gone: drop its change-detection state
            self._emit(EventKind.RETURN, code, frame_id, code.co_firstlineno)
        except Exception:
            self._dropped += 1
        return None

    def _on_raise(self, code: CodeType, instruction_offset: int, exception: BaseException) -> Any:
        """An exception surfaced. Emits only where it was BORN.

        CPython re-fires RAISE in every frame the exception crosses. Emitting all
        of them would make day 29's "where did this come from?" point at the frame
        the user is already looking at.
        """
        try:
            if not self._scope.allows(code.co_filename):
                return None  # NOT DISABLE: this event rejects it (see the note above)
            exc_id = id(exception)
            if exc_id == self._propagating:
                return None  # same exception, one frame further up: not an origin
            self._propagating = exc_id
            self._emit(
                EventKind.RAISE,
                code,
                self._frames.id_of(sys._getframe(1)),
                code.co_firstlineno,
                exc_type_id=self._exc_types.intern(type(exception).__name__),
            )
        except Exception:
            self._dropped += 1
        return None

    def _on_unwind(self, code: CodeType, instruction_offset: int, exception: BaseException) -> Any:
        """A frame is exiting because of an exception.

        Pops like PY_RETURN but stays a distinct kind. Treating it as a normal
        return would tell day 27's call tree that a frame which blew up finished
        cleanly -- erasing the most useful thing a call tree shows. Not popping at
        all would leak a frame per exception, forever.
        """
        try:
            if not self._scope.allows(code.co_filename):
                return None  # NOT DISABLE: this event rejects it (see the note above)
            frame_id = self._frames.exit(sys._getframe(1))
            self._last_ref.pop(frame_id, None)  # frame gone: drop its change-detection state
            self._emit(
                EventKind.UNWIND,
                code,
                frame_id,
                code.co_firstlineno,
                exc_type_id=self._exc_types.intern(type(exception).__name__),
            )
        except Exception:
            self._dropped += 1
        return None

    def _on_handled(self, code: CodeType, instruction_offset: int, exception: BaseException) -> Any:
        """An exception was caught. Bounds the unwind and ends the propagation."""
        try:
            if not self._scope.allows(code.co_filename):
                return None  # NOT DISABLE: this event rejects it (see the note above)
            self._propagating = None
            self._emit(
                EventKind.EXCEPTION_HANDLED,
                code,
                self._frames.id_of(sys._getframe(1)),
                code.co_firstlineno,
                exc_type_id=self._exc_types.intern(type(exception).__name__),
            )
        except Exception:
            self._dropped += 1
        return None

    def _emit(
        self,
        kind: EventKind,
        code: CodeType,
        frame_id: int,
        lineno: int,
        exc_type_id: int | None = None,
        name_id: int | None = None,
        value_ref: ValueRef | None = None,
    ) -> None:
        """The one place an Event is built. Every callback routes through here."""
        self.sink.emit(
            Event(
                seq=next(self._seq),
                kind=kind,
                timestamp_ns=time.perf_counter_ns(),
                thread_id=threading.get_ident(),
                frame_id=frame_id,
                code_id=self._codes.intern(code),
                lineno=lineno,
                exc_type_id=exc_type_id,
                name_id=name_id,
                value_ref=value_ref,
            )
        )

    def _callbacks(self) -> tuple[tuple[int, Any], ...]:
        """The event-to-handler wiring, in one place.

        Registration and teardown both walk this, so a new event cannot be
        registered and then forgotten on the release path -- which would leave a
        dangling callback on a tool id we no longer own.
        """
        e = sys.monitoring.events
        return (
            (e.LINE, self._on_line),
            (e.PY_START, self._on_start),
            (e.PY_RESUME, self._on_resume),
            (e.PY_YIELD, self._on_yield),
            (e.PY_RETURN, self._on_return),
            (e.RAISE, self._on_raise),
            (e.PY_UNWIND, self._on_unwind),
            (e.EXCEPTION_HANDLED, self._on_handled),
        )

    def _event_mask(self) -> int:
        mask = 0
        for event, _ in self._callbacks():
            mask |= event
        return mask

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
