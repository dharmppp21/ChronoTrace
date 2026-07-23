"""*"Where did this value come from?"* -- the exact write, then a labelled guess at its inputs.

**Exactly what this can and cannot see, first.** ChronoTrace records writes, not reads, so
the exact, always-correct answer is *the write that produced this value* and the full frame
state there -- the inputs are visible to a human looking at that instant. On top of that,
optionally, comes a *heuristic*: parse the writing line's source, find the names it reads,
and resolve each to its own last write. That second half is approximate -- it is blind
through function calls and attribute chains, it reads a two-line window because a value is
recorded as visible one line after the assignment that produced it (locals are captured per
line), and it is refused entirely against a source file that changed since recording -- so
it is presented as **"likely inputs", never "the cause"**. An approximation shown honestly
is a feature; shown as truth it is a lie, and this query's whole design is that distinction.

Interface: `ValueProvenanceQuery(name, seq)`, run via `execute`.

It must never know: how a jump renders. It returns instants, annotated with their role.

The result shape
----------------
The first `Hit` is the exact write, and its `note` carries the heuristic's status (verified,
or unavailable and why). The rest are the likely inputs, each `note`d as a heuristic. A read
that is a builtin, or a parameter written by an unrecorded caller, is simply not shown --
the heuristic cannot point at an instant for it, and inventing one would be the lie.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronotrace.index.var_writes import last_write_before
from chronotrace.query._ast_reads import SourceUnavailable, reads_on_line
from chronotrace.query._resolve import Location, frame_location, line_of, name_id, value_preview
from chronotrace.query.types import PAGE_SIZE, Cursor, Hit, QueryContext, QueryResult, UnknownName


@dataclass(frozen=True, slots=True)
class ValueProvenanceQuery:
    """The write that produced `name` at `seq`, plus a heuristic guess at that write's inputs.

    Attributes:
        name: the variable whose value you are asking about, as typed.
        seq: the instant you are looking at it. The producing write is the last one before it.
    """

    name: str
    seq: int

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """The exact producing write, then its likely inputs. Raises `UnknownName` on a typo."""
        if cursor is not None:
            return QueryResult.empty(ctx.partial)
        target = name_id(ctx.db, self.name)
        frame_id = ctx.reconstructor.reconstruct(self.seq).current_frame_id or None
        write = last_write_before(ctx.db, target, self.seq, frame_id=frame_id)
        scope = ""
        if write is None and frame_id is not None:
            write = last_write_before(ctx.db, target, self.seq)  # a global or closure cell
            scope = " (resolved outside the current frame -- a global or closure)"
        if write is None:
            return QueryResult.empty(ctx.partial)

        write_seq, write_frame, value_ref = write
        seen: dict[int, Location] = {}
        function, file = frame_location(ctx.db, int(write_frame), seen)
        inputs, status = self._inputs(ctx, int(write_seq), int(write_frame), file, seen)
        exact = Hit(
            seq=int(write_seq),
            file=file,
            lineno=line_of(ctx, int(write_seq)),
            function=function,
            value_preview=value_preview(ctx, int(value_ref)),
            note=f"the write that set {self.name!r}{scope}; {status}",
        )
        return QueryResult((exact, *inputs), None, ctx.partial)

    def _inputs(
        self,
        ctx: QueryContext,
        write_seq: int,
        write_frame: int,
        file: str | None,
        seen: dict[int, Location],
    ) -> tuple[list[Hit], str]:
        """The likely-input hits for the writing line, and a status describing their trust.

        Returns no hits (and an explaining status) when the source cannot be verified -- the
        exact write above still stands, which is the point of keeping the two separate.
        """
        lineno = line_of(ctx, write_seq)
        expected = ctx.reader.strings().hash_of(file) if file is not None else None
        try:
            names = _reads_near(file, lineno, expected) if file is not None else frozenset()
        except SourceUnavailable as exc:
            return [], f"likely-input analysis unavailable: {exc}"
        hits = [
            hit
            for read in sorted(names)
            if (hit := self._input_hit(ctx, read, write_seq, write_frame, seen)) is not None
        ]
        status = (
            "the inputs below are a HEURISTIC from the source line, not recorded dataflow"
            if hits
            else "no traceable local inputs on the source line"
        )
        return hits, status

    def _input_hit(
        self,
        ctx: QueryContext,
        read: str,
        write_seq: int,
        write_frame: int,
        seen: dict[int, Location],
    ) -> Hit | None:
        """One read name resolved to its own last write, or None if it cannot be traced.

        None for a builtin (never a recorded name) or a name with no write in this frame -- a
        parameter from an unrecorded caller, or a global the heuristic must not chase.
        """
        try:
            read_id = name_id(ctx.db, read)
        except UnknownName:
            return None
        earlier = last_write_before(ctx.db, read_id, write_seq, frame_id=write_frame)
        if earlier is None:
            return None
        seq, frame_id, value_ref = earlier
        function, file = frame_location(ctx.db, int(frame_id), seen)
        return Hit(
            seq=int(seq),
            file=file,
            lineno=line_of(ctx, int(seq)),
            function=function,
            value_preview=value_preview(ctx, int(value_ref)),
            note=f"likely input {read!r} -- heuristic",
        )


def _reads_near(file: str, lineno: int, expected: str | None) -> frozenset[str]:
    """Names read on the write's line and the line just before it.

    A binding's new value first appears one line *after* the assignment that produced it --
    locals are captured per LINE event, so a `result = n * 2` on line 18 is recorded as a
    write on line 19. Reading the two-line window catches the real inputs in the common
    straight-line case; it is a heuristic (already labelled so), and a slightly wider net is
    the right trade for a "likely inputs" feature. `SourceUnavailable` from either line
    propagates -- an unverifiable file is unverifiable for both.
    """
    return reads_on_line(file, lineno, expected) | reads_on_line(file, max(1, lineno - 1), expected)
