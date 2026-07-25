"""`render_captured` turns day-7 tagged data back into Python-shaped text, honestly.

The honesty that matters: a truncated container ends in `...` and a summary marker renders
as `<budget>` -- the preview never looks complete when the recording only saw part of it.
"""

from __future__ import annotations

from chronotrace.query._resolve import render_captured


def test_atoms_render_as_their_repr() -> None:
    assert render_captured(10) == "10"
    assert render_captured("hi") == "'hi'"
    assert render_captured(None) == "None"
    assert render_captured(True) == "True"


def test_containers_render_as_python() -> None:
    assert render_captured({"$": "list", "items": [1, 2], "len": 2}) == "[1, 2]"
    assert render_captured({"$": "tuple", "items": [1], "len": 1}) == "(1)"
    d = {"$": "dict", "items": [["a", 1], ["b", 2]], "len": 2}
    assert render_captured(d) == "{'a': 1, 'b': 2}"


def test_nesting_renders_recursively() -> None:
    inner = {"$": "list", "items": ["x"], "len": 1}
    outer = {"$": "dict", "items": [["k", inner]], "len": 1}
    assert render_captured(outer) == "{'k': ['x']}"


def test_a_truncated_container_ends_in_ellipsis() -> None:
    """The whole point: never render a prefix as if it were the whole thing."""
    trunc = {"$": "list", "items": [1, 2], "len": 1000, "truncated": True}
    assert render_captured(trunc) == "[1, 2, ...]"


def test_a_truncated_string_shows_its_length() -> None:
    s = {"$": "str", "head": "abc", "len": 5000, "truncated": True}
    assert render_captured(s) == "'abc'...(5000 str)"


def test_summary_markers_render_as_such() -> None:
    assert render_captured({"$": "budget"}) == "<budget>"
    assert render_captured({"$": "cycle", "id": None}) == "<cycle>"


def test_an_object_shows_its_type() -> None:
    obj = {"$": "obj", "type": "Point", "module": "m", "attrs": {"x": 1}}
    assert render_captured(obj) == "Point(...)"
