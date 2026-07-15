"""Where events go. The recorder's only exit.

The recorder must not know that a file format exists (see the package docstring's
layering rule), so it emits into a `Sink` and stops caring. Phase 1 ships one
implementation, in memory. Day 12 adds `FileSink` in `chronotrace.store`, which
is the *reason* this boundary exists today rather than being extracted later.

Why `Protocol` and not an ABC
-----------------------------
`typing.Protocol` gives structural typing: `FileSink` will satisfy `Sink` without
importing it or inheriting from it. That matters here specifically because of the
dependency rule -- `store` may know about `recorder`, but making `store.FileSink`
inherit `recorder.Sink` would put an inheritance edge across a layer boundary and
invite the base class to grow behaviour that both layers then share. A Protocol is
a contract with no runtime coupling: nothing to inherit, nothing to import,
nothing to accidentally reuse.

An ABC would also demand `Sink` be *the* base class of every sink forever. A test
double is then a subclass rather than any object with the right two methods.
"""

from __future__ import annotations

from typing import Protocol

from chronotrace.recorder.events import Event


class Sink(Protocol):
    """Accepts events. Implementations decide what that means.

    Implementations must tolerate `emit` being called from any thread and from
    inside a `sys.monitoring` callback -- which means they must not raise. A
    callback that raises propagates the exception into the program under
    observation, at whatever line happened to be executing; a debugger that
    injects exceptions into the program it is watching is worse than no debugger.
    Day 5 wires the recorder's own guard, but the contract starts here.
    """

    def emit(self, event: Event) -> None:
        """Accept one event. Must not raise.

        Args:
            event: the event. Treat it as immutable; it is a historical fact.
        """
        ...

    def close(self) -> None:
        """Release resources. Idempotent.

        Idempotent because the recorder closes on both the normal and the
        exception path, and a double close must not be a second failure on top of
        the first.
        """
        ...


class MemorySink:
    """Keeps events in a list. The Phase 1 sink.

    Deliberately does nothing clever. It is a scaffold: day 10 records 1M events
    into it and asserts a memory ceiling, and that assertion is *expected* to be
    tight -- at a measured 151 B/event a million events cost ~151 MB. That number
    is the argument for Phase 2's file store, so the scaffold's job is partly to
    demonstrate its own inadequacy with a number.

    Not thread-safe by locking; `list.append` is atomic under CPython's GIL, which
    is what makes this safe today and what PEP 703's free-threaded build removes.
    Flagged in `events.py`'s seq discussion; day 5 owns the counter that actually
    needs the atomicity.
    """

    __slots__ = ("_closed", "_events")

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._closed = False

    def emit(self, event: Event) -> None:
        """Append the event.

        Args:
            event: the event to keep.

        Complexity: O(1) amortised.
        """
        self._events.append(event)

    def close(self) -> None:
        """Mark closed. Idempotent; keeps the events readable."""
        self._closed = True

    @property
    def events(self) -> list[Event]:
        """The events, in emission order.

        Returns:
            The live list -- not a copy. Copying a million events to satisfy
            encapsulation would allocate 151 MB to protect a scaffold that day 12
            replaces. Callers are inside this project and are trusted not to
            mutate history.
        """
        return self._events
