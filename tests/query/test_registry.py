"""The query catalogue: enumerable without importing, and every target actually resolves.

The point of these is the lazy `module:attr` strings -- a typo in one would only surface
when a user ran that query. Loading every registered target here moves that failure to CI.
"""

from __future__ import annotations

import pytest

from chronotrace.query import registry


def test_every_registered_query_loads_and_is_runnable() -> None:
    """Each registered name resolves to a class with an `execute` -- no dead `module:attr`."""
    names = registry.names()
    assert names, "the registry must list at least the shipped queries"
    for name in names:
        cls = registry.load(name)
        assert callable(getattr(cls, "execute", None)), f"{name} -> {cls} is not runnable"


def test_summaries_cover_exactly_the_registered_names() -> None:
    """`--list` and the loader speak of the same set -- neither carries a name the other lacks."""
    assert set(registry.summaries()) == set(registry.names())


def test_an_unknown_query_name_is_a_keyerror() -> None:
    """The contract the CLI relies on to turn a bad name into a listing of the good ones."""
    with pytest.raises(KeyError):
        registry.load("no-such-query")
