"""Turns a running Python program into a stream of typed events.

This package owns everything that touches the program under observation:
``sys.monitoring`` callbacks (PEP 669), the frame model, bounded value capture,
value deduplication, scope filtering and redaction. It is the only layer that
runs *inside* the user's hot path -- every microsecond here is paid once per
line of their program.

Public surface
--------------
Exported below and nothing else. A small public API is a maintenance asset, not
modesty: everything named here is a promise to every layer above, and a promise
is expensive to withdraw. `InternTable` and `ValuePool` stay unexported -- they
are how the recorder does its job, not what it offers. Import them by their full
path if you genuinely need them; the friction is the point.

The rest of Phase 1 (days 5-10) adds the `Recorder` itself, frame tracking, value
capture and scope filtering.

What this package must NEVER import
-----------------------------------
``chronotrace.store``, ``.index``, ``.reconstruct``, ``.query``, ``.server``.

The recorder is the bottom of the dependency order, and that is a design
constraint rather than an accident: it must be usable with any sink (day 4
defines a ``Sink`` protocol; day 12 adds the file-backed implementation), and it
must not know that a storage format exists at all. If the recorder ever needs to
know how bytes reach disk, the abstraction has failed.
"""

from chronotrace.recorder.events import Event, EventKind
from chronotrace.recorder.recorder import Recorder
from chronotrace.recorder.sink import MemorySink, Sink
from chronotrace.recorder.values import ValueRef

__all__ = ["Event", "EventKind", "MemorySink", "Recorder", "Sink", "ValueRef"]
