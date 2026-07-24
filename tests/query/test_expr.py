"""The restricted evaluator: the grammar it supports, and its three-valued honesty.

The security half is `test_expr_security.py`; this proves the arithmetic, comparisons, and
-- most importantly -- that a value the recording only summarised evaluates to UNKNOWN
(`None`), never a confident `False`.
"""

from __future__ import annotations

from typing import Any

import pytest

from chronotrace.query.expr import ConditionError, compile_condition


def ev(src: str, **bindings: Any) -> bool | None:
    return compile_condition(src).evaluate(bindings)


def test_comparisons() -> None:
    assert ev("i > 100", i=200) is True
    assert ev("i > 100", i=50) is False
    assert ev("i == 5", i=5) is True
    assert ev("i != 5", i=6) is True


def test_chained_comparison() -> None:
    assert ev("0 < i < 10", i=5) is True
    assert ev("0 < i < 10", i=15) is False
    assert ev("0 < i < 10", i=0) is False


def test_boolean_ops() -> None:
    assert ev("a and b", a=True, b=True) is True
    assert ev("a and b", a=True, b=False) is False
    assert ev("a or b", a=False, b=True) is True
    assert ev("not done", done=False) is True


def test_arithmetic() -> None:
    assert ev("x + 1 > 10", x=10) is True
    assert ev("x * 2 == 8", x=4) is True
    assert ev("x % 2 == 0", x=6) is True


def test_membership_in_a_literal_collection() -> None:
    assert ev("i in [1, 2, 3]", i=2) is True
    assert ev("i in [1, 2, 3]", i=9) is False
    assert ev("i not in [1, 2, 3]", i=9) is True


def test_a_name_not_in_scope_is_unknown_not_false() -> None:
    """The rule: absence is not falsehood. `i > 100` with no `i` is unknown."""
    assert ev("i > 100") is None


def test_index_into_a_captured_list() -> None:
    xs = {"$": "list", "items": [10, 20, 30], "len": 3}
    assert ev("xs[0] == 10", xs=xs) is True
    assert ev("xs[1] > 15", xs=xs) is True
    assert ev("xs[-1] == 30", xs=xs) is True


def test_membership_in_a_captured_list() -> None:
    xs = {"$": "list", "items": [1, 2, 3], "len": 3}
    assert ev("2 in xs", xs=xs) is True
    assert ev("9 in xs", xs=xs) is False


def test_attribute_on_a_captured_object() -> None:
    p = {"$": "obj", "type": "Point", "module": "m", "attrs": {"x": 5, "y": 9}}
    assert ev("p.x == 5", p=p) is True
    assert ev("p.y > 100", p=p) is False


def test_a_truncated_value_is_unknown_never_a_confident_false() -> None:
    """The day's honesty rule, in the evaluator. A prefix cannot answer about the whole."""
    xs = {"$": "list", "items": [1, 2], "len": 1000, "truncated": True}
    assert ev("xs == [1, 2]", xs=xs) is None, "cannot confirm equality of a prefix"
    assert ev("5 in xs", xs=xs) is None, "5 may be in the tail we did not capture"
    assert ev("1 in xs", xs=xs) is True, "but 1 is visible -- definitely present"


def test_summary_markers_are_unknown() -> None:
    for marker in ({"$": "budget"}, {"$": "depth", "type": "dict"}, {"$": "redacted"}):
        assert ev("v > 0", v=marker) is None
        assert ev("v == 1", v=marker) is None


def test_a_missing_key_or_out_of_range_index_is_unknown() -> None:
    d = {"$": "dict", "items": [["a", 1]], "len": 1}
    assert ev("d['a'] == 1", d=d) is True
    assert ev("d['b'] == 2", d=d) is None, "a key we do not have is unknown"
    xs = {"$": "list", "items": [1, 2], "len": 100, "truncated": True}
    assert ev("xs[50] == 3", xs=xs) is None, "past the captured prefix is unknown"


def test_kleene_logic_lets_a_definite_operand_dominate() -> None:
    """`False and unknown` is False; `True or unknown` is True -- the unknown cannot flip it."""
    mystery = {"$": "budget"}
    assert ev("known and mystery", known=False, mystery=mystery) is False
    assert ev("known or mystery", known=True, mystery=mystery) is True
    assert ev("known and mystery", known=True, mystery=mystery) is None
    assert ev("known or mystery", known=False, mystery=mystery) is None


def test_a_syntax_error_is_a_condition_error() -> None:
    with pytest.raises(ConditionError):
        compile_condition("i >")


def test_the_free_names_are_collected_for_pushdown() -> None:
    """The breakpoint query narrows on these; they must be exactly the variables read."""
    assert compile_condition("i > 100 and x[0] == y").names == frozenset({"i", "x", "y"})
