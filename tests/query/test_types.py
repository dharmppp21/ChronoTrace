"""The query vocabulary: the cursor arithmetic, and that a fake context is really injectable."""

from __future__ import annotations

from chronotrace.query import Cursor, Hit, QueryContext, QueryResult, VarWritesQuery

from .conftest import fake_ctx, synthetic_db


def _hits(*seqs: int) -> list[Hit]:
    return [Hit(seq=s) for s in seqs]


def test_a_full_page_has_no_next_cursor() -> None:
    """Fewer rows than the fetch limit means the result set is exhausted -- do not page on."""
    result = QueryResult.page(_hits(0, 1, 2), limit=5, partial=False)
    assert result.hits == (Hit(0), Hit(1), Hit(2))
    assert result.next_cursor is None


def test_an_overflowing_page_drops_the_sentinel_and_points_past_the_last_kept() -> None:
    """`limit + 1` rows means there is more; the cursor is the last *kept* seq, not the extra.

    Pointing at the sentinel would skip it on the next page; pointing at the last kept row
    means the next page resumes strictly after what the caller has already seen.
    """
    result = QueryResult.page(_hits(10, 11, 12, 13, 14, 15), limit=5, partial=False)
    assert [h.seq for h in result.hits] == [10, 11, 12, 13, 14]
    assert result.next_cursor == Cursor(14)


def test_an_empty_page_is_complete_not_a_dangling_cursor() -> None:
    """No rows is a valid, finished answer -- never a cursor that would loop forever."""
    result = QueryResult.page([], limit=5, partial=False)
    assert result.hits == ()
    assert result.next_cursor is None


def test_a_query_runs_against_a_hand_built_fake_context() -> None:
    """The injection is real: a query answers against a synthetic index and a stub reader.

    No recording, no `.open` -- just `QueryContext(reader, db)` built by hand. If this could
    not be written, `QueryContext` would be injecting nothing.
    """
    db = synthetic_db()
    db.execute("INSERT INTO strings(id, text) VALUES (1, 'x')")
    db.executemany(
        "INSERT INTO var_writes(name_id, seq, frame_id, value_ref) VALUES (1, ?, 0, ?)",
        [(5, 42), (9, 99)],
    )
    result = VarWritesQuery("x").execute(fake_ctx(db))
    assert [h.seq for h in result.hits] == [5, 9]
    # the stub reader returns the ref as the value, so the preview is its repr
    assert [h.value_preview for h in result.hits] == ["42", "99"]


def test_partial_recording_makes_every_result_partial() -> None:
    """A truncated reader flags the result partial without any query knowing how to check."""
    db = synthetic_db()
    db.execute("INSERT INTO strings(id, text) VALUES (1, 'x')")
    db.execute("INSERT INTO var_writes(name_id, seq, frame_id, value_ref) VALUES (1, 5, 0, 42)")
    ctx: QueryContext = fake_ctx(db, truncated=True)
    assert VarWritesQuery("x").execute(ctx).partial is True
