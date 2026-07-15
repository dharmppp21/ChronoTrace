"""The event vocabulary every layer of ChronoTrace speaks.

The recorder emits these, the store persists them, the indexer reads them, the
reconstructor replays them, and the API serves them. Seven layers touch these
types, which makes this the most expensive file in the project to change. It is
designed as a **wire protocol**, not as convenient Python objects.

The `seq` contract
------------------
Every event carries `seq`: a strictly increasing integer assigned by the recorder
**in emission order**, starting at 0. It is the address of an instant in time and
the primary key of the entire system. The index is keyed on it, queries return
it, reconstruction takes it as its only argument, the UI playhead *is* it, and it
ends up in the browser's URL.

`seq` is assigned by an in-process counter and is never derived from the
timestamp. Two reasons, both fatal:

* **Clocks are not monotonic.** NTP steps, suspend/resume and VM migration can move
  a wall clock backwards. `time.monotonic_ns` fixes that but still cannot fix:
* **Two events can share a timestamp.** Timer resolution is finite; a tight loop
  emits several events per tick. Ties mean no total order, and "the previous
  instant" stops being well-defined -- which is the one question this project
  exists to answer.

`timestamp_ns` is therefore *data* (the UI shows durations, day 27 buckets by
time), never *identity*.

Flat records, not a union per kind
----------------------------------
One `Event` type carries a `kind` tag plus fields that only some kinds use. A
union of per-kind classes would be more type-safe and is the obvious OO instinct,
but it is wrong here: day 12 encodes events as **columns** (all `seq` together,
all `lineno` together) because that is what makes delta and run-length encoding
work. Ragged columns from a union would defeat it. A tagged flat record is what
every wire protocol converges on, for this reason.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from chronotrace.recorder.values import ValueRef


class EventKind(enum.IntEnum):
    """What happened. Each kind earns its place by powering a named capability.

    `IntEnum` rather than `Enum`: the value is written to disk and compared in
    SQL, so it must *be* an int, not merely wrap one.

    Deliberately absent -- `VAR_READ`. Recording every attribute and name read
    would multiply event volume several-fold (reads vastly outnumber writes) to
    answer "who looked at x?", a question nobody asks while debugging. `VAR_WRITE`
    answers "who *changed* x?", which is the question people actually ask, and it
    is what day 29's provenance query needs. If exact dataflow is ever required,
    day 29's AST heuristic reconstructs reads from the source line instead --
    approximate, but free. Revisit only if that heuristic proves inadequate in
    real use.
    """

    LINE = 1
    """A source line executed. Powers the timeline and the line-hit index."""

    CALL = 2
    """A Python frame was pushed. Powers the call tree."""

    RETURN = 3
    """A frame returned normally. Powers the call tree."""

    RAISE = 4
    """An exception was born here. **Origins only.**

    Day 6 correction, from measurement: CPython's `RAISE` event fires in *every*
    frame an exception passes through, not only where it started. An exception
    crossing three frames produces::

        RAISE boom   <- origin
        UNWIND boom
        RAISE mid    <- same exception, re-reported
        UNWIND mid
        RAISE outer  <- again
        HANDLED outer

    Day 4's docstring claimed `RAISE` meant "the origin". It did not, and day 29's
    flagship query ("where did this exception come from?") would have pointed at
    whichever frame the user was already looking at.

    The recorder therefore emits `RAISE` only for the **first** sighting of an
    exception object and suppresses the rest. Nothing is lost: propagation is
    fully described by `UNWIND` (a frame exited because of it) and
    `EXCEPTION_HANDLED` (where it stopped). A non-origin `RAISE` carries no
    information those two lack, and this is the hot path.
    """

    UNWIND = 5
    """A frame is exiting *because of* an exception -- propagation, not origin.

    A separate kind rather than a flag on RETURN. Day 27's call-tree index stores
    `exit_kind` per frame and colours abnormal exits, and the difference between
    "returned" and "blew up" is the single most useful thing a call tree can show.
    Two kinds keep that a column comparison rather than a bitmask test, and keep
    the columnar encoder (day 12) packing one small int.
    """

    EXCEPTION_HANDLED = 6
    """An exception was caught. Bounds the unwind and tells the UI where it stopped."""

    YIELD = 7
    """A generator or coroutine suspended."""

    RESUME = 8
    """A generator or coroutine resumed.

    YIELD/RESUME exist because a suspended frame breaks any stack model. A stack
    says a frame is entered once and exited once, LIFO. A generator is entered,
    leaves without exiting, and re-enters later -- possibly interleaved with other
    frames under asyncio. Without these events the call tree silently reports that
    a generator's frame lives from first call to final exhaustion, hiding every
    suspension. Day 6 replaces the stack with a live-frame registry for exactly
    this reason.
    """

    VAR_WRITE = 9
    """A local binding changed. Powers "every mutation of x" and provenance."""


@dataclass(slots=True, frozen=True)
class Event:
    """One thing that happened, at one instant.

    Frozen because an event is a historical fact: nothing downstream may edit the
    past. `slots=True` because there will be millions -- measured at 151 B/event
    against 191 B without slots (`benchmarks/RESULTS.md`).

    Attributes:
        seq: the instant. Strictly increasing, assigned in emission order. See
            the module docstring; this is the project's primary key.
        kind: what happened.
        timestamp_ns: `time.perf_counter_ns()` at emission. Data, never identity.
        thread_id: emitting thread.
        frame_id: which *frame*, from a monotonic counter -- never `id(frame)`.
            CPython reuses ids after GC (proven in `spikes/RESULTS-capture.md`),
            so `id()` would fuse two unrelated frames into one node of the call
            tree. Per-frame, not per-code-object: recursion has many live frames
            sharing one code object.
        code_id: interned code object. One id rather than separate file/func ids
            because `sys.monitoring` hands us the code object directly and
            filename, qualname and first line are all derivable from it -- so this
            is one intern lookup per event in the hot path instead of two.
        lineno: source line, or 0 where the kind has no line.
        name_id: interned variable name. VAR_WRITE only; None elsewhere.
        value_ref: the new value. VAR_WRITE only; None elsewhere. An event never
            embeds a value -- see `values.py` for why the indirection matters.
        exc_type_id: interned exception type name (`"ValueError"`). RAISE, UNWIND
            and EXCEPTION_HANDLED only; None elsewhere. The *type* rather than the
            exception object because a type name is a short repeated string that
            interns to a small int, which is exactly what day 12's columnar
            encoder wants. The message is a value and belongs to day 7's capture,
            not to a second string field here.
    """

    seq: int
    kind: EventKind
    timestamp_ns: int
    thread_id: int
    frame_id: int
    code_id: int
    lineno: int = 0
    name_id: int | None = None
    value_ref: ValueRef | None = None
    exc_type_id: int | None = None
