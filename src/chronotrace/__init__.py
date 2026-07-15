"""ChronoTrace: a time-travel debugger for Python.

Records a program's entire execution -- every line, call, return, exception and
local-variable value -- into a compressed, memory-mapped log, then lets a
developer scrub backward through it and ask causal questions about the past.

Layering
--------
Dependencies point one way only::

    server -> query -> reconstruct -> index -> store -> recorder

A module may import from layers *below* it and never from layers above. Nothing
below may leak its types through the public API of something above. Day 10 makes
this an automated import-graph test rather than a convention, because a rule
nobody enforces is a preference.

Why this module imports almost nothing
--------------------------------------
``chronotrace`` is imported *into the process being debugged*. Anything this
package imports at module scope appears in the user's ``sys.modules``, competes
with their own dependency versions, and costs their program startup time. So the
top-level package exposes the version and nothing else; every subsystem is
imported explicitly by the caller that needs it.
"""

from chronotrace._version import __version__

__all__ = ["__version__"]
