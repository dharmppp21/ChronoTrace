"""Resolution helpers shared by the queries: name -> id, frame -> location, ref -> preview.

Extracted because four queries (`var-writes`, `last-write`, `provenance`, and the callers
view) all turn the same raw ids into the same display, and three copies of "resolve the
value or say <deleted>" is exactly the duplicated logic the project forbids. These are the
small, boring conversions every query does at its edges; keeping them in one place means a
fix to how a value previews, or how a frame names itself, lands everywhere at once.

It must never know what a query *means* -- only how to turn an id into text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from chronotrace.index.var_writes import DELETED
from chronotrace.query.types import UnknownName

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
    """A short `repr` of a written value -- `<deleted>` for a `del`, None if the pool lost it.

    A `del x` is stored as a row with no value (day 24) and is a real answer to "who wrote
    x", so it is shown, not skipped. A value the pool has lost is a corrupt recording, not a
    `None`: it renders as absent, never as a fake value.
    """
    if value_ref == DELETED:
        return "<deleted>"
    from chronotrace.reconstruct import MissingValue

    try:
        text = repr(ctx.resolver.resolve(value_ref))
    except MissingValue:
        return None
    return text if len(text) <= MAX_PREVIEW_CHARS else f"{text[:MAX_PREVIEW_CHARS]}..."
