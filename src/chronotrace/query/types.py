"""The query layer's vocabulary: **a query result is a set of instants you can jump to**.

That sentence is the whole product thesis. Reconstruction (phase 3) answers *"what was
the state at instant S?"*; the index (phase 4) precomputes *"when did X happen?"*. This
layer turns those into *answers a person asks* -- and the answer to every one of them is a
set of `seq` numbers. `seq` is the universal address (day 4): given one, every other layer
can land you there -- the scrubber, the variable panel, the call stack. So a query that
returns text is a `grep`; a query that returns **instants** is a debugger. `QueryResult` is
designed around that: its core is `seq`, and everything else on a `Hit` is only there so a
human can decide *which* instant to jump to before they click.

What this layer owns
--------------------
The typed query API (`Query`, `QueryContext`, `QueryResult`), the queries themselves, and
the promise that no query can OOM the caller (cursor pagination, below). It reads the index
and the recording through the layers below; it computes no new facts, it only *asks*.

What it must never know
-----------------------
`server` and the frontend -- how a result is rendered, what the user clicked, what a URL
looks like. It returns `seq`-addressed data; turning that into a clickable line is the
layer above's job. The dependency arrow points down.

No DSL -- a typed API, and the restraint is the point
-----------------------------------------------------
The tempting thing today is a query *language*: `"writes to total where seq < 500"`. It is
the wrong thing, and expensively so. A DSL needs a lexer, a grammar, a parser, error
messages good enough to debug, documentation, and -- the moment anyone scripts against it
-- a stability promise that outlives every internal refactor. All of that has to be built
*before* we know which queries people actually run. A typed callable with typed results
costs none of it: the type checker is the parser, the IDE is the documentation, and a
signature change is caught at the call site instead of at a user's runtime. So we ship the
typed API, learn which queries matter, and revisit a DSL only when the trigger fires
(issue #13): a real user composing three or more queries by hand, repeatedly, because the
API cannot express what they mean. Until then a DSL is generality with no demand.

Cursor pagination, not `OFFSET`, and never an unbounded list
------------------------------------------------------------
"Every write to `i`" in a hot loop is ten million rows. A query that returns them all can
exhaust the UI's memory, so no query here returns an unbounded list -- results come one
`limit`-sized page at a time. The cursor is a `seq`, not an `OFFSET`, and that is a
correctness choice as much as a speed one: `OFFSET n` re-reads and discards the first `n`
rows on every page, so paging to the end is O(rows^2); a `seq` cursor is `WHERE seq > ?`,
an indexed seek that costs the same for page one and page ten thousand. And because `seq`
is unique and monotonic, the cursor can neither skip a row nor return one twice -- the two
failure modes an `OFFSET` (or any non-unique key) walks straight into.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from chronotrace.index import Progress
    from chronotrace.reconstruct import KeyframeReconstructor, ValueResolver
    from chronotrace.store import ChronoReader

PAGE_SIZE = 100
"""Rows per page when a caller does not say otherwise. Large enough that the common
"show me the writes to this variable" is one round trip, small enough that a page's worth
of value previews is cheap to resolve -- the enrichment cost is bounded by this, not by
the size of the result set."""


class QueryError(Exception):
    """A query could not run as asked. Distinct from a query that ran and found nothing."""


class UnknownName(QueryError):
    """The variable name was never recorded -- a typo, not an empty result.

    "no writes to `total`" and "there is no `total`" are different answers: the first is a
    fact about the program, the second is a fact about the question. Conflating them sends
    a user hunting for a bug in their program when the bug is in what they typed.
    """


class UnknownFile(QueryError):
    """The file is not in this recording -- as opposed to a line in it that never ran."""


@dataclass(frozen=True, slots=True)
class Cursor:
    """Where the next page resumes: strictly after this `seq`.

    A `seq`, never an offset. Unique and monotonic, so paging can neither skip nor repeat.
    """

    after_seq: int


def after_bound(cursor: Cursor | None) -> int:
    """The exclusive lower `seq` bound a page resumes from -- every query filters `seq > this`.

    `None` (the first page) resolves to `-1`, so `seq > -1` includes `seq` 0. Centralised so
    the sentinel is defined once rather than reappearing as a bare `-1` in every query.
    """
    return cursor.after_seq if cursor is not None else -1


@dataclass(frozen=True, slots=True)
class Hit:
    """One instant a query found, plus just enough context to choose it before jumping.

    `seq` is the answer; the rest is display, and every display field is optional because
    each query fills only what its index cheaply knows (a variable write knows the value it
    wrote; a line hit knows the file and line). A missing field is "not applicable to this
    query", rendered as nothing -- never guessed.
    """

    seq: int
    file: str | None = None
    lineno: int | None = None
    function: str | None = None
    value_preview: str | None = None


@dataclass(frozen=True, slots=True)
class QueryResult:
    """One page of instants, a cursor to the next page, and whether the answer is complete.

    `partial` is not decoration: a query over a crash-truncated recording answers only for
    the events that survived, and silently returning fewer hits than exist would be the one
    thing a debugger must never do -- under-report without saying so. The flag makes the
    incompleteness part of the result, so the layer above can show it.
    """

    hits: tuple[Hit, ...]
    next_cursor: Cursor | None
    partial: bool

    @classmethod
    def page(cls, hits: list[Hit], *, limit: int, partial: bool) -> QueryResult:
        """Build a page from `limit + 1` candidate rows, deriving the cursor from the extra.

        The caller fetches one more row than it needs; if that row exists there is another
        page, and the cursor is the last *kept* hit's `seq`. Fetching the sentinel row is
        how "is there a next page?" is answered without a second `COUNT` query, and doing
        the arithmetic here means no individual query re-derives (and mis-derives) it.
        """
        more = len(hits) > limit
        kept = hits[:limit]
        cursor = Cursor(kept[-1].seq) if more and kept else None
        return cls(tuple(kept), cursor, partial)


class Query(Protocol):
    """A typed, parameterised question. Constructed with its arguments; run against a context.

    The parameters are constructor arguments (`VarWritesQuery(name="total")`), so the type
    checker validates the question; the resources are injected at `execute`, so the same
    query object can run against a real recording or a fake. That split is the design: what
    the query *asks* is immutable and typed, what it asks *of* is supplied from outside.
    """

    def execute(
        self, ctx: QueryContext, cursor: Cursor | None = None, *, limit: int = PAGE_SIZE
    ) -> QueryResult:
        """Answer, one page at a time. `cursor` resumes a previous page; None starts."""
        ...


class QueryContext:
    """The resources a query runs against, bundled and injected -- not opened per query.

    Injected rather than each query opening its own reader and connection, for two reasons
    the design leans on:

    * **Resource lifetime.** One recording is one mmap and one SQLite connection, opened
      once and closed once. A query engine where every query opened its own would leak file
      handles under a stream of questions, and on Windows a lingering handle blocks the
      index's own rebuild (issue #10). One owner, one close.
    * **Testability.** A query's logic is separable from where its data lives only if the
      data can be swapped. A `QueryContext` built by hand around a fake reader is how a test
      drives a query without a real recording -- and if a query could not be handed a fake
      context, the injection would be a fiction. `test_types.py` builds one to prove it.

    `reconstructor` and `resolver` are built lazily from the reader on first use, so a query
    that only needs the index (a line hit) never pays to construct them, and a fake context
    for a query that does not touch them needs only a reader stub.
    """

    def __init__(self, reader: ChronoReader, db: sqlite3.Connection) -> None:
        self.reader = reader
        self.db = db

    @cached_property
    def resolver(self) -> ValueResolver:
        """Resolves `value_ref`s to captured values, for a `Hit`'s value preview."""
        from chronotrace.reconstruct import ValueResolver

        return ValueResolver(self.reader)

    @cached_property
    def reconstructor(self) -> KeyframeReconstructor:
        """Program state at an instant -- for the causal queries that land day 29."""
        from chronotrace.reconstruct import KeyframeReconstructor

        return KeyframeReconstructor(self.reader)

    @property
    def partial(self) -> bool:
        """Whether the recording is crash-truncated -- every result over it is partial."""
        return self.reader.truncated

    @classmethod
    def open(
        cls, recording: Path, *, on_progress: Callable[[Progress], None] | None = None
    ) -> QueryContext:
        """Open a recording and its index, building the index if it is missing or stale.

        This is ADR-0008's lazy fallback made real: a query does not require an index to
        exist, it requires one to be *available*, and the honest way to make it available is
        to build it now (reporting progress, because it can take real seconds). A stale
        index -- wrong fingerprint, old schema -- is discarded and rebuilt rather than
        trusted, which is the entire reason staleness is stamped.
        """
        from chronotrace.store import ChronoReader

        reader = ChronoReader.open(recording)
        try:
            db = _open_index(recording, reader, on_progress)
        except BaseException:
            reader.close()
            raise
        return cls(reader, db)

    def close(self) -> None:
        """Close the connection and the recording. Idempotent enough to call in `finally`."""
        self.db.close()
        self.reader.close()

    def __enter__(self) -> QueryContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _open_index(
    recording: Path,
    reader: ChronoReader,
    on_progress: Callable[[Progress], None] | None,
) -> sqlite3.Connection:
    """Return a connection to a current index for `recording`, building one if needed.

    A present-and-current index is opened directly. A missing or stale one is (re)built
    from the recording -- the only source of truth -- and then opened. The staleness check
    is what makes a silently-outdated index impossible: it is cheaper to rebuild than to
    serve a wrong answer from.
    """
    import sqlite3

    from chronotrace.index import build_index
    from chronotrace.index.db import sidecar_path
    from chronotrace.index.schema import staleness

    path = sidecar_path(recording)
    if path.exists():
        connection = sqlite3.connect(path)
        if staleness(connection, recording) is None:
            return connection
        connection.close()
    build_index(recording, reader, on_progress=on_progress)
    return sqlite3.connect(sidecar_path(recording))
