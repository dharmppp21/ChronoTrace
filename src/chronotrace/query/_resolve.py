"""Resolution helpers shared by the queries: name -> id, frame -> location, ref -> preview.

Extracted because four queries (`var-writes`, `last-write`, `provenance`, and the callers
view) all turn the same raw ids into the same display, and three copies of "resolve the
value or say <deleted>" is exactly the duplicated logic the project forbids. These are the
small, boring conversions every query does at its edges; keeping them in one place means a
fix to how a value previews, or how a frame names itself, lands everywhere at once.

It must never know what a query *means* -- only how to turn an id into text.
"""

from __future__ import annotations

import os.path
from pathlib import Path
from typing import TYPE_CHECKING, cast

from chronotrace.index.var_writes import DELETED
from chronotrace.query.types import UnknownFile, UnknownName

if TYPE_CHECKING:
    import sqlite3

    from chronotrace.query.types import QueryContext
    from chronotrace.recorder.events import Event

MAX_PREVIEW_CHARS = 120
"""How much of a value's `repr` a `Hit` carries. The capture policy already bounds the
value; this bounds the *preview line*, so one big dict cannot fill a result page."""

_LOCATION = (
    "SELECT c.qualname, f.path FROM frames fr "
    "JOIN codes c ON fr.code_id = c.code_id "
    "JOIN files f ON c.file_id = f.file_id WHERE fr.frame_id = ?"
)

Location = tuple[str | None, str | None]
"""`(function, file)` for a frame -- either may be None when the frame predates the
recording or its code was never interned."""


def name_id(db: sqlite3.Connection, name: str) -> int:
    """The recording's id for a variable name, or reject it as a typo -- not an empty result.

    Raises:
        UnknownName: the name was never recorded. Distinct from "recorded but never
            written", which is a valid empty result -- see `types.UnknownName`.
    """
    row = db.execute("SELECT id FROM strings WHERE text = ?", (name,)).fetchone()
    if row is None:
        raise UnknownName(f"no variable named {name!r} was recorded")
    return int(row[0])


def _path_key(path: str) -> str:
    r"""A path as the platform actually compares paths -- the key for matching, never for display.

    Paths reach us as raw text from two places that spell the same file differently: the
    recording interned whatever `__file__` held, while the user types whatever their shell
    completed. On Windows those disagree constantly -- `C:/src/app.py` and `C:\src\app.py`
    are one file, and so is `c:\SRC\App.py`, but all three are distinct strings. Comparing
    the raw text made a correct query report "no file matching", which reads as "your
    recording is wrong" when the recording was fine.

    `os.path.normcase` is exactly this: it folds separators and case on Windows and is the
    identity on POSIX, where `/` is the only separator and case is significant. Wrapping
    `normpath` first also collapses `./` and redundant separators. Doing this by hand --
    `.replace("\\", "/").lower()` -- would be wrong on POSIX, where a backslash is a legal
    character in a filename and case matters.
    """
    return os.path.normcase(os.path.normpath(path))


def resolve_file(db: sqlite3.Connection, name: str) -> tuple[int, str]:
    """The one interned file the user meant, by full path or basename, as `(file_id, path)`.

    A bare filename is what a user reading source types, so it is accepted -- but if two
    recorded files share it the question is ambiguous, and guessing one would answer about
    the wrong file. An exact path always wins over a basename match. Matching is
    platform-aware (see `_path_key`), so on Windows the separator and case a user typed do
    not decide whether their file is found.

    Raises:
        UnknownFile: nothing matched, or a bare name matched several files.
    """
    rows = db.execute("SELECT file_id, path FROM files").fetchall()
    # The returned path is always the one the RECORDING interned, never the normalised key --
    # results should echo the file as it was recorded, not a lowercased rewrite of it.
    wanted = _path_key(name)
    exact = [(int(fid), path) for fid, path in rows if _path_key(path) == wanted]
    if exact:
        return exact[0]
    by_name = [(int(fid), path) for fid, path in rows if _path_key(Path(path).name) == wanted]
    if not by_name:
        raise UnknownFile(f"no file matching {name!r} is in this recording")
    if len(by_name) > 1:
        paths = ", ".join(sorted(path for _fid, path in by_name))
        raise UnknownFile(f"{name!r} is ambiguous; use a fuller path -- matches: {paths}")
    return by_name[0]


def frame_location(db: sqlite3.Connection, frame_id: int, cache: dict[int, Location]) -> Location:
    """`(function, file)` for a frame, memoised across a page -- writes share frames."""
    if frame_id not in cache:
        row = db.execute(_LOCATION, (frame_id,)).fetchone()
        cache[frame_id] = (row[0], row[1]) if row is not None else (None, None)
    return cache[frame_id]


def line_of(ctx: QueryContext, seq: int) -> int:
    """The source line of the event at `seq`. An int key returns one `Event`, never a list."""
    return cast("Event", ctx.reader[seq]).lineno


def value_preview(ctx: QueryContext, value_ref: int) -> str | None:
    """A short, Python-shaped preview of a written value -- `<deleted>` for a `del`, None if lost.

    A `del x` is stored as a row with no value (day 24) and is a real answer to "who wrote
    x", so it is shown, not skipped. A value the pool has lost is a corrupt recording, not a
    `None`: it renders as absent, never as a fake value.
    """
    if value_ref == DELETED:
        return "<deleted>"
    from chronotrace.reconstruct import MissingValue

    try:
        text = render_captured(ctx.resolver.resolve(value_ref))
    except MissingValue:
        return None
    return text if len(text) <= MAX_PREVIEW_CHARS else f"{text[:MAX_PREVIEW_CHARS]}..."


def render_captured(value: object) -> str:
    """A captured value as Python-shaped text: `['first']`, not `{'$': 'list', 'items': ...}`.

    The day-7 capture stores containers as tagged dicts so they are pure data; that shape is
    right on disk and wrong on screen. This unwraps it back to how the value looked -- the
    dogfooding session (day 31) showed the raw tag leaking into every result was the single
    worst readability problem. A truncated container ends in `...`, a summary marker renders
    as `<budget>`/`<cycle>`, so the preview never pretends to be complete when it is not.
    """
    if not (isinstance(value, dict) and "$" in value):
        return repr(value)  # an atom: int, str, bool, None -- captured unwrapped
    tag = value["$"]
    if tag in _SUMMARY:
        return f"<{tag}>"
    if tag == "dict":
        body = ", ".join(f"{render_captured(k)}: {render_captured(v)}" for k, v in value["items"])
        return "{" + _ellipsis(body, value) + "}"
    if tag in _SEQ:
        return _SEQ[tag] % _ellipsis(", ".join(render_captured(v) for v in value["items"]), value)
    if tag == "bytes" and "v" in value:
        return repr(bytes.fromhex(value["v"]))  # a short bytes value, captured whole
    if tag in ("str", "bytes"):
        return f"{value['head']!r}...({value['len']} {tag})"  # a truncated string/bytes prefix
    return f"{value.get('type', tag)}(...)"  # an object: its type, state elided


_SUMMARY = frozenset({"budget", "depth", "cycle", "redacted"})
_SEQ = {"list": "[%s]", "tuple": "(%s)", "set": "{%s}", "frozenset": "frozenset({%s})"}


def _ellipsis(body: str, value: dict[str, object]) -> str:
    """The rendered items, with a trailing `...` when the container was truncated."""
    if not value.get("truncated"):
        return body
    return f"{body}, ..." if body else "..."
