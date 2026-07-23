"""The flagship query: golden results, frame scoping, the typo/empty split, and pagination.

The pagination test is a property (a random page size walks the whole set exactly once) and
the latency test asserts the contract on ten million rows -- both because a query engine
that can silently drop a row or stall the UI is not done, it is a demo.
"""

from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path

import pytest

from chronotrace.index import schema
from chronotrace.index.db import connect
from chronotrace.query import Cursor, QueryContext, UnknownName, VarWritesQuery

from .conftest import fake_ctx, synthetic_db

LATENCY_BUDGET_S = 0.050
"""p95 of a page query on a 10M-event recording. Stated as a contract and asserted, because
a query engine without a latency budget is a benchmark waiting to embarrass you."""


def test_writes_to_total_reflect_the_recorders_change_detection(simple_ctx: QueryContext) -> None:
    """`total` is set to 0, then `+= quadruple(0)` (still 0), then `+= quadruple(1)` (4).

    Two writes, not three: day 8's content-addressed dedup fires a VAR_WRITE only when a
    binding's value actually *changes*, so the redundant 0 -> 0 assignment records nothing.
    The query reflects the recording faithfully -- it answers what happened, not what the
    source text says should have. Read off the recorder's rule, so a disagreement indicts
    the index, not the expectation.
    """
    result = VarWritesQuery("total").execute(simple_ctx)
    assert [h.value_preview for h in result.hits] == ["0", "4"]
    assert all(h.function == "main" for h in result.hits)
    assert all(h.file is not None and h.file.endswith("simple.py") for h in result.hits)
    assert result.next_cursor is None
    assert result.partial is False


def test_frame_scoping_isolates_one_invocation(simple_ctx: QueryContext) -> None:
    """`result` is written once in each of `double`'s four calls -- four frames, one name.

    Unscoped, the query answers across all four; scoped to one `frame_id` it answers about
    that invocation alone. This is the recursion case that a name-only key would get wrong.
    """
    db = simple_ctx.db
    (name_id,) = db.execute("SELECT id FROM strings WHERE text = 'result'").fetchone()
    frame_ids = [
        f for (f,) in db.execute("SELECT frame_id FROM var_writes WHERE name_id=?", (name_id,))
    ]
    assert len(frame_ids) == 4, "double runs four times"

    assert len(VarWritesQuery("result").execute(simple_ctx).hits) == 4
    scoped = VarWritesQuery("result", frame_id=frame_ids[0]).execute(simple_ctx)
    assert len(scoped.hits) == 1


def test_an_unknown_name_is_a_typo_not_an_empty_result(simple_ctx: QueryContext) -> None:
    """A name that was never recorded is a different answer from "no writes" -- it raises."""
    with pytest.raises(UnknownName):
        VarWritesQuery("no_such_variable_xyz").execute(simple_ctx)


def test_a_name_with_no_writes_in_range_is_empty_not_an_error(simple_ctx: QueryContext) -> None:
    """`total` exists, but nothing was written before its own first write: an empty result."""
    first = VarWritesQuery("total").execute(simple_ctx).hits[0].seq
    empty = VarWritesQuery("total", before_seq=first).execute(simple_ctx)
    assert empty.hits == ()
    assert empty.next_cursor is None


def test_cursor_pagination_walks_every_row_once_at_any_page_size() -> None:
    """The property that makes pagination correct: no gaps, no duplicates, at any `limit`.

    250 writes with arbitrary `seq` gaps, walked with random page sizes. The concatenation
    of every page must equal the full set, in order -- which fails if the cursor ever skips
    a row (points past a kept one) or repeats one (points before the last kept one).
    """
    db = synthetic_db()
    db.execute("INSERT INTO strings(id, text) VALUES (1, 'x')")
    seqs = list(range(0, 1000, 4))[:250]
    db.executemany(
        "INSERT INTO var_writes(name_id, seq, frame_id, value_ref) VALUES (1, ?, 0, ?)",
        [(s, s) for s in seqs],
    )
    ctx = fake_ctx(db)
    rng = random.Random(0)  # noqa: S311 -- test determinism, not security
    for _ in range(25):
        limit = rng.randint(1, 300)
        collected = _walk(ctx, limit)
        assert collected == seqs, f"page size {limit} did not return every row exactly once"


def _walk(ctx: QueryContext, limit: int) -> list[int]:
    """Page through every write to `x`, following cursors, collecting the seqs in order."""
    collected: list[int] = []
    cursor: Cursor | None = None
    for _ in range(10_000):  # a terminating guard: a good cursor ends long before this
        result = VarWritesQuery("x").execute(ctx, cursor, limit=limit)
        assert len(result.hits) <= limit
        collected.extend(h.seq for h in result.hits)
        if result.next_cursor is None:
            return collected
        cursor = result.next_cursor
    raise AssertionError("cursor never terminated")


@pytest.fixture(scope="module")
def ten_million_rows(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 10M-row index, built once for the whole module. The latency contract's fixture.

    Rows are spread across 500 names so the target name holds a realistic fraction while the
    B-tree it seeks through is genuinely 10M deep -- the seek cost this asserts is O(log n),
    and n must actually be large for the assertion to mean anything.
    """
    path = tmp_path_factory.mktemp("big") / "big.idx"
    db = connect(path)
    schema.create(db)
    db.execute("INSERT INTO strings(id, text) VALUES (1, 'x')")
    total, names = 10_000_000, 500
    db.executemany(
        "INSERT INTO var_writes(name_id, seq, frame_id, value_ref) VALUES (?,?,0,?)",
        ((seq % names + 1, seq, seq) for seq in range(total)),
    )
    db.commit()
    db.close()
    return path


def test_a_page_query_meets_its_latency_budget_on_ten_million_events(
    ten_million_rows: Path,
) -> None:
    """p95 of a page fetch stays under the budget, and the plan proves the index is used.

    A budget met by a full scan is luck; `EXPLAIN QUERY PLAN` must show a keyed search, so
    the guarantee survives a recording ten times larger.
    """
    db = sqlite3.connect(ten_million_rows)
    ctx = fake_ctx(db)
    rng = random.Random(1)  # noqa: S311 -- test determinism, not security

    timings = []
    for _ in range(200):
        after = Cursor(rng.randrange(10_000_000))
        start = time.perf_counter()
        VarWritesQuery("x").execute(ctx, after, limit=100)
        timings.append(time.perf_counter() - start)
    timings.sort()
    p95 = timings[int(0.95 * len(timings))]
    assert p95 < LATENCY_BUDGET_S, f"p95 {p95 * 1e3:.1f} ms exceeds {LATENCY_BUDGET_S * 1e3:.0f} ms"

    plan = " ".join(
        str(row[-1])
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT seq, frame_id, value_ref FROM var_writes "
            "WHERE name_id=? AND seq>? ORDER BY seq LIMIT ?",
            (1, 0, 100),
        )
    )
    assert "SCAN" not in plan.upper(), plan
    db.close()
