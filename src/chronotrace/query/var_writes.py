"""The flagship query: *"who wrote `total`, and to what?"* -- as jumpable instants.

Problem this solves: the question a debugger exists to answer is "where did this value come
from?", and the honest answer is not a number, it is an *instant* -- the moment the write
happened, which you then jump to and inspect. This query returns those instants, each
carrying enough context (which function, what value) to pick the one you meant before you
land on it.

Interface: `VarWritesQuery(name, frame_id=None, before_seq=None)`, run via `execute`.

It must never know: how a result is rendered or what "jump" means. It returns `seq`s.

Built on the day-26 index
-------------------------
Every fact here comes from `var_writes`, clustered on `(name_id, seq)`, so a page is an
indexed range scan already in `seq` order -- no sort. The value preview and the function
name are enrichment, resolved only for the page actually returned, so the cost of decoding
values is bounded by `limit` and never by how many times the variable was written.

`frame_id` is why recursion does not lie
----------------------------------------
`total` in one call and `total` in another are different variables sharing a name. Without
`frame_id` the query would merge them and answer "who wrote total" with some other call's
write. Passing `frame_id` scopes the question to one invocation; omitting it asks across
all of them, newest instants last. Day 6's per-frame identity is what makes the distinction
real rather than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chronotrace.index.var_writes import DELETED
from chronotrace.query.types import (
    PAGE_SIZE,
    Cursor,
    Hit,
    QueryContext,
    QueryResult,
    UnknownName,
    after_bound,
)

if TYPE_CHECKING:
    import sqlite3

MAX_PREVIEW_CHARS = 120
"""How much of a value's `repr` a `Hit` carries. The capture policy already bounds the
value; this bounds the *preview line*, so one big dict cannot fill a result page."""

_LOCATION = (
    "SELECT c.qualname, f.path FROM frames fr "
    "JOIN codes c ON fr.code_id = c.code_id "
    "JOIN files f ON c.file_id = f.file_id WHERE fr.frame_id = ?"
)


@dataclass(frozen=True, slots=True)
class VarWritesQuery:
    """Every write to `name`, oldest instant first; optionally one frame, before one instant.

    Attributes:
        name: the variable, as the user typed it. Resolved to the recording's `name_id`.
        frame_id: restrict to a single invocation (necessary under recursion).
        before_seq: only writes strictly before this instant -- "who last set this, before
            it went wrong?" is `before_seq` plus reading the last page.
    """

    name: str
    frame_id: int | None = None
    before_seq: int | None = None

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """Return one page of writes. Raises `UnknownName` if the variable never existed."""
        name_id = self._name_id(ctx)
        rows = self._page(ctx.db, name_id, cursor, limit)
        seen: dict[int, tuple[str | None, str | None]] = {}
        hits = [
            self._hit(ctx, int(seq), int(frame_id), int(value_ref), seen)
            for seq, frame_id, value_ref in rows
        ]
        return QueryResult.page(hits, limit=limit, partial=ctx.partial)

    def _hit(
        self,
        ctx: QueryContext,
        seq: int,
        frame_id: int,
        value_ref: int,
        seen: dict[int, tuple[str | None, str | None]],
    ) -> Hit:
        """One write turned into a jumpable instant: where it happened, what it wrote."""
        function, file = _location(ctx.db, frame_id, seen)
        return Hit(seq=seq, function=function, file=file, value_preview=_preview(ctx, value_ref))

    def _name_id(self, ctx: QueryContext) -> int:
        """Resolve the typed name to its id, or reject it as a typo -- not an empty result."""
        row = ctx.db.execute("SELECT id FROM strings WHERE text = ?", (self.name,)).fetchone()
        if row is None:
            raise UnknownName(f"no variable named {self.name!r} was recorded")
        return int(row[0])

    def _page(
        self, db: sqlite3.Connection, name_id: int, cursor: Cursor | None, limit: int
    ) -> list[tuple[int, int, int]]:
        """One `limit + 1` page of `(seq, frame_id, value_ref)`, in `seq` order.

        The extra row is the pagination sentinel (`QueryResult.page`). The `WHERE` clauses
        are assembled from literal fragments and bound parameters -- no user text enters the
        SQL -- so the composition is safe despite the f-string.
        """
        after = after_bound(cursor)
        conds = ["name_id = ?", "seq > ?"]
        params: list[int] = [name_id, after]
        if self.frame_id is not None:
            conds.append("frame_id = ?")
            params.append(self.frame_id)
        if self.before_seq is not None:
            conds.append("seq < ?")
            params.append(self.before_seq)
        params.append(limit + 1)
        where = " AND ".join(conds)  # literal fragments only; every value is a bound param
        sql = (
            f"SELECT seq, frame_id, value_ref FROM var_writes "  # noqa: S608
            f"WHERE {where} ORDER BY seq LIMIT ?"
        )
        return list(db.execute(sql, params))


def _location(
    db: sqlite3.Connection, frame_id: int, cache: dict[int, tuple[str | None, str | None]]
) -> tuple[str | None, str | None]:
    """`(function, file)` for a frame, memoised across a page -- writes share frames."""
    if frame_id not in cache:
        row = db.execute(_LOCATION, (frame_id,)).fetchone()
        cache[frame_id] = (row[0], row[1]) if row is not None else (None, None)
    return cache[frame_id]


def _preview(ctx: QueryContext, value_ref: int) -> str | None:
    """A short `repr` of the written value -- or `<deleted>` for a `del`, None if lost.

    A `del x` is stored as a row with no value (day 24), and it is a real answer to "who
    last wrote x", so it is shown rather than skipped. A value the pool has lost is a
    corrupt recording, not a `None`: it renders as absent, never as a fake value.
    """
    if value_ref == DELETED:
        return "<deleted>"
    from chronotrace.reconstruct import MissingValue

    try:
        text = repr(ctx.resolver.resolve(value_ref))
    except MissingValue:
        return None
    return text if len(text) <= MAX_PREVIEW_CHARS else f"{text[:MAX_PREVIEW_CHARS]}..."
