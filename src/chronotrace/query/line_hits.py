"""*"Every time line 47 ran"* -- a retroactive breakpoint, as a list of instants.

Problem this solves: a breakpoint on a program that already finished cannot *stop*
anything. The time-travel form of "break on line 47" is a query: the instants where that
line executed, each jumpable, steppable in either direction. Day 30 builds the stepping
command on top; this returns the answer.

Interface: `LineHitsQuery(file, lineno)`, run via `execute`.

It must never know: what a breakpoint *is*. It returns the `seq`s where a line ran.

Built on the day-27 index
-------------------------
`line_hits` is clustered on `(file_id, lineno, seq)`, so a page is a range scan already in
`seq` order. There is no value to preview and no single function to name (a line can belong
to a comprehension nested in a `def`), so a hit carries the file and line the user asked
for and the instant they did not know -- which is the whole answer.

Three situations, and the two we can honestly tell apart
--------------------------------------------------------
A user naming a file and line can be in three states, and the index distinguishes two of
them cleanly:

* **the file is not in the recording** -- a typo or the wrong path: `UnknownFile`, a
  different kind of answer from "found nothing".
* **the file is here and the line executed** -- the hits.
* **the file is here and the line has no hits** -- an *empty* result. This is where "the
  line never ran" and "there is no such line (a comment, a blank, past the end)" collapse
  into one, because telling them apart needs the *source*, which the index deliberately
  does not store (a recording must be readable on a machine that never had the program).
  So rather than fabricate a third message we cannot stand behind, the empty result names
  the real ambiguity, and the source pane (day 35) will resolve it when source is at hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronotrace.query._resolve import resolve_file
from chronotrace.query.types import PAGE_SIZE, Cursor, Hit, QueryContext, QueryResult, after_bound


@dataclass(frozen=True, slots=True)
class LineHitsQuery:
    """Every instant `file:lineno` executed, oldest first.

    Attributes:
        file: a path or a bare filename (`pipeline.py`). Matched against the recording's
            interned paths; a bare name is fine unless two recorded files share it.
        lineno: the 1-based source line.
    """

    file: str
    lineno: int

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """Return one page of hits. Raises `UnknownFile` if the file is not in the recording."""
        file_id, path = resolve_file(ctx.db, self.file)
        after = after_bound(cursor)
        rows = ctx.db.execute(
            "SELECT seq FROM line_hits WHERE file_id = ? AND lineno = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (file_id, self.lineno, after, limit + 1),
        ).fetchall()
        hits = [Hit(seq=int(seq), file=path, lineno=self.lineno) for (seq,) in rows]
        return QueryResult.page(hits, limit=limit, partial=ctx.partial)
