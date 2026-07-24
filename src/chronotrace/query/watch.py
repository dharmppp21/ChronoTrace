"""Watchpoints: every instant a variable changed, old value -> new value.

Problem this solves: *"stop whenever `total` changes"* -- the watchpoint a debugger sets and
then single-steps forever hoping to catch. Here it is a query: the day-8 recorder already
emits a `VAR_WRITE` only when a binding's value actually changes (content-addressed dedup),
so the change history *is* the `var_writes` index, and each change's *old* value is simply
the previous change's *new* value. The invertible deltas of day 16 store old refs explicitly
and would serve too, but reaching into the store's delta blocks from the query layer is the
heavier path; the index the query layer already speaks answers this directly.

Interface: `WatchQuery(name, frame_id=None, changed_to=ANY, changed_from=ANY)`.

It must never know: how a jump renders. It returns change instants with old/new previews.

`--changed-to` / `--changed-from` are honest about truncation
-------------------------------------------------------------
A filter compares a recorded value against a literal. If the recorded value was summarised
(a truncated list, a redacted secret), it cannot be confirmed equal -- so the change is not
returned under a filter, and never wrongly matched. An unfiltered watch shows every change,
truncation and all, because there the summary is the honest answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chronotrace.index.var_writes import DELETED, last_write_before
from chronotrace.query._resolve import name_id, value_preview
from chronotrace.query.expr import UNKNOWN, to_python
from chronotrace.query.types import PAGE_SIZE, Cursor, Hit, QueryContext, QueryResult, after_bound

if TYPE_CHECKING:
    import sqlite3

ANY = object()
"""No filter on this side of the change -- distinct from a filter whose target is `None`."""

_BATCH = 2048


@dataclass(frozen=True, slots=True)
class WatchQuery:
    """Every instant `name` changed, oldest first, each as `old -> new`.

    Attributes:
        name: the variable to watch, as typed. Resolved to the recording's `name_id`.
        frame_id: restrict to one invocation; omitted, the whole timeline of the name.
        changed_to / changed_from: keep only changes whose new / old value equals this
            (a plain Python value). `ANY` means no filter on that side.
    """

    name: str
    frame_id: int | None = None
    changed_to: Any = ANY
    changed_from: Any = ANY

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """One page of changes. Raises `UnknownName` if the variable was never recorded."""
        nid = name_id(ctx.db, self.name)
        after = after_bound(cursor)
        prev_ref = self._ref_before(ctx.db, nid, after)
        hits: list[Hit] = []
        for seq, frame_id, value_ref in self._changes_after(ctx.db, nid, after):
            if len(hits) > limit:
                break
            hit = self._hit(ctx, int(seq), int(frame_id), prev_ref, int(value_ref))
            prev_ref = int(value_ref)
            if hit is not None:
                hits.append(hit)
        return QueryResult.page(hits, limit=limit, partial=ctx.partial)

    def _hit(
        self, ctx: QueryContext, seq: int, frame_id: int, old_ref: int | None, new_ref: int
    ) -> Hit | None:
        """One change as `old -> new`, or None if a `--changed-*` filter excludes it."""
        if not self._passes(ctx, new_ref, self.changed_to):
            return None
        if old_ref is not None and not self._passes(ctx, old_ref, self.changed_from):
            return None
        if old_ref is None and self.changed_from is not ANY:
            return None  # a first write has no old value to match against
        old = "unset" if old_ref is None else _preview(ctx, old_ref)
        return Hit(
            seq=seq,
            value_preview=f"{old} -> {_preview(ctx, new_ref)}",
            note=f"{self.name} changed",
        )

    def _passes(self, ctx: QueryContext, ref: int, target: Any) -> bool:
        """Whether the value at `ref` equals `target` -- False if summarised (cannot confirm)."""
        if target is ANY:
            return True
        if ref == DELETED:
            return False  # a deletion has no value equal to any literal
        scalar = to_python(ctx.resolver.resolve(ref))
        return scalar is not UNKNOWN and scalar == target

    def _ref_before(self, db: sqlite3.Connection, nid: int, after: int) -> int | None:
        """The value_ref of the change just before the page, so the first row has an old value."""
        prior = last_write_before(db, nid, after + 1, frame_id=self.frame_id)
        return None if prior is None else int(prior[2])

    def _changes_after(self, db: sqlite3.Connection, nid: int, after: int) -> Any:
        """Yield `(seq, frame_id, value_ref)` for the name's changes after `after`, lazily."""
        frame = "AND frame_id=? " if self.frame_id is not None else ""
        sql = (
            f"SELECT seq, frame_id, value_ref FROM var_writes "  # noqa: S608
            f"WHERE name_id=? {frame}AND seq>? ORDER BY seq LIMIT ?"
        )
        cursor = after
        while True:
            head = (nid, self.frame_id) if self.frame_id is not None else (nid,)
            rows = db.execute(sql, (*head, cursor, _BATCH)).fetchall()
            for row in rows:
                yield int(row[0]), int(row[1]), int(row[2])
            if len(rows) < _BATCH:
                return
            cursor = int(rows[-1][0])


def _preview(ctx: QueryContext, ref: int) -> str:
    """The value at `ref` as a short display string. `value_preview` already says `<deleted>`."""
    return value_preview(ctx, ref) or "<unavailable>"
