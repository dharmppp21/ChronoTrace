"""*"Where did this exception really come from?"* -- to the birth, and back to the root.

This is a question no Python traceback answers. A traceback shows you the frames an
exception *crossed* and the line it *surfaced* on; it does not show the program state at the
instant it was **born**, and for a chained exception (`raise X from Y`) it prints Y's text
but cannot take you to Y's birthplace with its locals intact. ChronoTrace can jump you to
both, because it recorded them.

Interface: `ExceptionOriginQuery(seq)`, run via `execute`.

It must never know: how a jump happens. It returns the origin instants, in chain order.

Two walks, one query
--------------------
1. **To the birth.** `sys.monitoring` fires RAISE in every frame an exception crosses; the
   recorder marks only the first (day 6), so `index.origin_of` maps the instant you are
   standing at -- the crash, a propagation frame -- to the instant the exception came into
   existence. That single frame is where the locals that caused it still live.
2. **To the root.** From that birth, follow the recorded `__cause__`/`__context__` links
   (format 1.7, #11) to the exception that caused *it*, and so on, until a link points into
   unrecorded code or runs out. The last one is the root cause. Explicit chaining
   (`raise X from Y`) is preferred over implicit (an exception raised while handling
   another), matching how a traceback ranks "direct cause" over "during handling".

Result: one `Hit` per exception in the chain, symptom-origin first, root last, each a
jumpable instant carrying its type, its birth frame and line, and *how* it relates to the
one before it. An exception whose origin is not in the recording (raised in the stdlib, a C
extension) yields an empty result -- said plainly, never pointed at the nearest wrong frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chronotrace.index.exceptions import origin_of
from chronotrace.query._resolve import Location, frame_location, line_of
from chronotrace.query.types import PAGE_SIZE, Cursor, Hit, QueryContext, QueryResult

if TYPE_CHECKING:
    import sqlite3

_BORN = "born here -- the exception you asked about"
_CAUSE = "the direct cause (raise ... from ...)"
_CONTEXT = "raised while handling the above (__context__)"


@dataclass(frozen=True, slots=True)
class ExceptionOriginQuery:
    """The birth of the exception visible at `seq`, then its cause chain to the root.

    Attributes:
        seq: an instant where the exception is visible -- typically the crash, or any frame
            it propagated through. The query resolves it to the origin regardless.
    """

    seq: int

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """The chain from this exception's birth to its root cause. Empty if unrecorded.

        A chain is short and returned whole, so `cursor` (there is no next page) yields an
        empty result rather than re-walking.
        """
        if cursor is not None:
            return QueryResult.empty(ctx.partial)
        origin = origin_of(ctx.db, self.seq)
        if origin is None:
            return QueryResult.empty(ctx.partial)
        chain = _walk_chain(ctx.db, origin[0])
        seen: dict[int, Location] = {}
        hits = [self._hit(ctx, seq, note, seen) for seq, note in chain]
        return QueryResult(tuple(hits), None, ctx.partial)

    def _hit(self, ctx: QueryContext, seq: int, note: str, seen: dict[int, Location]) -> Hit:
        """One exception's birth as a jumpable instant: its type, frame, line and relation."""
        row = ctx.db.execute(
            "SELECT type_id, frame_id FROM exceptions WHERE seq = ?", (seq,)
        ).fetchone()
        type_id, frame_id = row
        function, file = frame_location(ctx.db, int(frame_id), seen)
        return Hit(
            seq=seq,
            file=file,
            lineno=line_of(ctx, seq),  # the source line the exception was raised on
            function=function,
            value_preview=_type_name(ctx.db, int(type_id)),
            note=note,
        )


def _walk_chain(db: sqlite3.Connection, origin_seq: int) -> list[tuple[int, str]]:
    """`(origin_seq, relation)` from the symptom's birth to the root, following the links.

    `__cause__` wins over `__context__` where both are set (`raise X from Y` records both,
    and the explicit one is what the user meant). The `seen` guard makes a pathological cycle
    -- which well-formed chains never contain -- terminate rather than loop.
    """
    chain = [(origin_seq, _BORN)]
    seen = {origin_seq}
    current = origin_seq
    while True:
        row = db.execute(
            "SELECT chained_cause_seq, chained_context_seq FROM exceptions WHERE seq = ?",
            (current,),
        ).fetchone()
        if row is None:
            break
        if row[0] is not None:
            nxt, relation = int(row[0]), _CAUSE
        elif row[1] is not None:
            nxt, relation = int(row[1]), _CONTEXT
        else:
            break
        if nxt in seen:
            break
        chain.append((nxt, relation))
        seen.add(nxt)
        current = nxt
    if len(chain) > 1:
        last_seq, last_note = chain[-1]
        chain[-1] = (last_seq, f"{last_note} -- the root cause")
    return chain


def _type_name(db: sqlite3.Connection, type_id: int) -> str:
    """The exception's type name (`"ValueError"`), or a placeholder if the string is lost."""
    row = db.execute("SELECT text FROM exc_types WHERE id = ?", (type_id,)).fetchone()
    return str(row[0]) if row is not None else f"exc_type#{type_id}"
