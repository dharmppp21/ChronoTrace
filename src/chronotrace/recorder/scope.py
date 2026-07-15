"""Decides whether a code object is ours to record.

Today this answers one question: *is this ChronoTrace's own code?* Recording
ourselves would be infinite regress. Day 9 grows the same mechanism into full
scope filtering (exclude the stdlib and site-packages), which is why the
interface takes a filename rather than being called `is_chronotrace`.

Why this is not needed for the callback itself
----------------------------------------------
`sys.monitoring` suppresses events while a callback is executing -- verified, not
assumed: a callback that calls a helper does not recurse and does not blow the
stack. So everything the callback touches (the sink, the intern tables) is
already excluded for free.

The window that *does* need excluding is narrow and real: our own code running
with monitoring enabled but **outside** a callback. That is exactly
`Recorder.stop()`, whose first lines execute before `set_events(0)` takes effect
and would otherwise appear in the user's recording as events from
`chronotrace/recorder/recorder.py`.

Why the check is keyed on the filename string
---------------------------------------------
The obvious key is the code object. It is the wrong one: code objects hash **by
value**, over their bytecode, measured at ~71 ns (`benchmarks/RESULTS.md`). A
filename is a `str`, and CPython caches a string's hash after first use -- and
every code object in a module shares one `co_filename` object, so the dict probe
usually hits on identity before it ever compares characters.

Day 2 measured that a naive per-event `co_filename != target` string comparison
costs 19-27% on code that is entirely in scope (`spikes/RESULTS-overhead.md`).
This cache turns that into one dict probe, and out-of-scope code pays even that
only once, because the caller returns `DISABLE` and the location de-instruments
itself.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = str(Path(__file__).parent.parent)


class Scope:
    """Answers "should this file be recorded?", cached per filename.

    Not thread-safe by locking, and does not need to be: the cache is a plain
    dict whose values are pure functions of the key, so a race can only cause two
    threads to compute the same answer and store it twice. `dict.__setitem__` is
    atomic under the GIL. Under PEP 703's free-threaded build the same reasoning
    holds -- a benign duplicate computation, never a wrong answer.
    """

    __slots__ = ("_cache", "_excluded_root")

    def __init__(self, excluded_root: str = _PACKAGE_ROOT) -> None:
        """Build a scope.

        Args:
            excluded_root: directory whose files are never recorded. Defaults to
                the installed `chronotrace` package. Injectable so tests can
                exercise the logic without pointing at the real package.
        """
        self._excluded_root = excluded_root
        self._cache: dict[str, bool] = {}

    def allows(self, filename: str) -> bool:
        """Whether events from `filename` should be recorded.

        Args:
            filename: a code object's `co_filename`. May be a synthetic name
                like `<string>` for `exec`-ed code, which is not a real path.

        Returns:
            False for ChronoTrace's own modules, True otherwise.

        Complexity: O(1) after first sight of a filename -- one dict probe,
        usually resolved on identity because every code object in a module shares
        one `co_filename` object. First sight costs one `startswith`.
        """
        cached = self._cache.get(filename)
        if cached is not None:
            return cached
        allowed = not filename.startswith(self._excluded_root)
        self._cache[filename] = allowed
        return allowed
