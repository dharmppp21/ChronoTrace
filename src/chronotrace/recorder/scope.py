"""Decides whether a code object is ours to record -- the biggest perf lever.

The problem this solves
-----------------------
Recording every line of the standard library and every third-party package is
both ruinously slow and useless: nobody debugging their own bug wants
`json/decoder.py` in the timeline. This module answers one question per code
object -- *should events from this file be recorded?* -- and it answers it once,
because the recorder turns the answer into `sys.monitoring.DISABLE` and CPython
then stops calling us for that location entirely.

`DISABLE`, not an in-callback filter
------------------------------------
The naive design checks scope inside the callback and returns early for
out-of-scope code -- but you still pay the callback dispatch on every stdlib line.
The recorder instead returns `sys.monitoring.DISABLE` (see recorder.py), which
de-instruments that code *location* so the callback is never invoked there again.
Day 2 measured a per-event `co_filename` comparison at 19-27% overhead on
in-scope-heavy code; `DISABLE` drops the out-of-scope cost to a single call per
location, once. That is the difference between "cheap per event" and "zero events".

Default-narrow, and why
-----------------------
"My code" defaults to the project's own directory tree; the stdlib and
site-packages are excluded even when a virtualenv lives *inside* the project (the
dominant `.venv/` layout -- prefix matching on the project root alone would record
all of site-packages). Default-narrow is faster, produces smaller recordings, and
matches what a developer means by "my code". A user debugging *into* a library
opts that library back in with an `--include` glob rather than everyone paying to
record everything by default.

Decision order (first match wins)
---------------------------------
1. ChronoTrace's own package -> never (infinite regress; non-negotiable).
2. An `exclude` glob matches -> no.
3. An `include` glob matches -> yes (this is how you record into a library).
4. Under the stdlib or site-packages -> no (the default library exclusion).
5. Under a project root -> yes. Otherwise no.

Synthetic filenames (`<string>`, `<frozen ...>`) are `exec`/`eval`/frozen code
with no on-disk source. They are excluded by default -- a timeline pointing at
line 4 of source that does not exist on disk is worse than silence -- and opt in
only by an explicit `include` glob matching the literal name.

Caching: keyed on the filename string
--------------------------------------
The obvious key is the code object; it is the wrong one. Code objects hash **by
value** over their bytecode (~71 ns, `benchmarks/RESULTS.md`). `co_filename` is a
`str` whose hash CPython caches after first use, and every code object in a module
shares one `co_filename` object, so the cache has one entry per *module* and the
probe usually resolves on identity before comparing a character. The scope answer
is a pure function of the filename and never changes for a file, so caching it is
always sound.
"""

from __future__ import annotations

import contextlib
import os
import site
import sysconfig
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase
from pathlib import Path

_CASEFOLD = os.name == "nt"  # Windows filesystems are case-insensitive; POSIX is not


def _normalize(path: str) -> str:
    """Absolute, forward-slashed, case-folded on Windows -- one canonical form.

    Uses `abspath`, never `realpath`: symlink resolution touches disk, and this
    runs (once per file) near the hot path. The cost is that a symlinked project
    layout may need an explicit `--include`; that is documented, not silently
    wrong.
    """
    # abspath, not Path.resolve(): resolve() follows symlinks and touches disk,
    # which the docstring above explains we deliberately avoid here.
    s = os.path.abspath(path).replace("\\", "/")  # noqa: PTH100
    return s.lower() if _CASEFOLD else s


def _normalize_glob(pattern: str) -> str:
    """A glob normalised to match `_normalize`d paths: forward slashes, folded case.

    So a user writes `*/migrations/*` with forward slashes and it matches on
    Windows too, where the real path is back-slashed and lower-cased.
    """
    p = pattern.replace("\\", "/")
    return p.lower() if _CASEFOLD else p


def _library_dirs() -> tuple[str, ...]:
    """The stdlib and site-packages directories, normalised, computed once.

    These are excluded by default even under a project root, because a virtualenv
    commonly lives inside the project and `site-packages` under the root would
    otherwise be recorded in full.
    """
    dirs: set[str] = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path:
            dirs.add(path)
    with contextlib.suppress(AttributeError):
        dirs.update(site.getsitepackages())  # some virtualenvs omit getsitepackages
    dirs.add(site.getusersitepackages())
    return tuple(_normalize(d) for d in dirs)


_LIBRARY_DIRS = _library_dirs()
_PACKAGE_ROOT = _normalize(str(Path(__file__).parent.parent))


def _under(path: str, root: str) -> bool:
    """True if `path` is `root` or lives beneath it. Boundary-aware.

    `startswith(root)` alone would put `/proj2` under `/proj`; the `/` boundary
    stops that, so a sibling directory sharing a name prefix is never captured.
    """
    return path == root or path.startswith(root + "/")


class Scope:
    """Answers "should this file be recorded?", cached per filename.

    Not thread-safe by locking, and does not need to be: the cache values are pure
    functions of the key, so a race can only recompute the same answer and store
    it twice. `dict.__setitem__` is atomic under the GIL, and the reasoning holds
    under free-threading too -- a benign duplicate, never a wrong answer.
    """

    __slots__ = ("_cache", "_exclude", "_include", "_roots")

    def __init__(
        self,
        roots: Sequence[str] | None = None,
        *,
        include: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> None:
        """Build a scope.

        Args:
            roots: directories whose files are "my code". Defaults to the current
                working directory. Pass the target script's directory from the CLI.
            include: globs that force a file into scope even if it is a library or
                outside every root -- how you debug into a dependency.
            exclude: globs that force a file out of scope even if under a root.

        Complexity: O(roots + globs) per *new* filename, O(1) cached thereafter.
        """
        chosen = list(roots) if roots is not None else [str(Path.cwd())]
        self._roots = tuple(_normalize(r) for r in chosen)
        self._include = tuple(_normalize_glob(g) for g in include)
        self._exclude = tuple(_normalize_glob(g) for g in exclude)
        self._cache: dict[str, bool] = {}

    def allows(self, filename: str) -> bool:
        """Whether events from `filename` should be recorded.

        Args:
            filename: a code object's `co_filename`, possibly synthetic
                (`<string>`) or a path that does not exist on disk (zipimport).

        Returns:
            True to record, False to `DISABLE`. See the module docstring for the
            decision order.

        Complexity: O(1) after first sight of a filename -- one dict probe, usually
        resolved on string identity because a module's code objects share one
        `co_filename`.
        """
        cached = self._cache.get(filename)
        if cached is not None:
            return cached
        allowed = self._decide(filename)
        self._cache[filename] = allowed
        return allowed

    def _decide(self, filename: str) -> bool:
        if filename.startswith("<") and filename.endswith(">"):
            # Synthetic: exec/eval/frozen. No disk source; opt in by literal only.
            return any(fnmatchcase(filename, p) for p in self._include)
        norm = _normalize(filename)
        if _under(norm, _PACKAGE_ROOT):
            return False
        if any(fnmatchcase(norm, p) for p in self._exclude):
            return False
        if any(fnmatchcase(norm, p) for p in self._include):
            return True
        if any(_under(norm, lib) for lib in _LIBRARY_DIRS):
            return False
        return any(_under(norm, r) for r in self._roots)
