"""Turns a running Python program into a stream of typed events.

This package owns everything that touches the program under observation:
``sys.monitoring`` callbacks (PEP 669), the frame model, bounded value capture,
value deduplication, scope filtering and redaction. It is the only layer that
runs *inside* the user's hot path -- every microsecond here is paid once per
line of their program.

Public surface
--------------
Filled in during Phase 1 (days 4-10). Today this package is an empty shell that
exists to pin the layer boundary before any code can violate it.

What this package must NEVER import
-----------------------------------
``chronotrace.store``, ``.index``, ``.reconstruct``, ``.query``, ``.server``.

The recorder is the bottom of the dependency order, and that is a design
constraint rather than an accident: it must be usable with any sink (day 4
defines a ``Sink`` protocol; day 12 adds the file-backed implementation), and it
must not know that a storage format exists at all. If the recorder ever needs to
know how bytes reach disk, the abstraction has failed.
"""
