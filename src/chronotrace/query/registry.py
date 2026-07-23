"""The catalogue of queries, by name -- enumerable without importing a single one.

Problem this solves: the CLI's `--list` and the day-33 server both need to answer "what
queries exist?" without paying to import every query module -- and there will be a dozen by
day 30, each dragging in its own SQL and helpers. Importing them all just to print a menu
is how a CLI acquires a visible startup lag.

Interface: `names`, `summaries`, `load`.

It must never know: what any query *does*. It maps a name to where its class lives and a
one-line summary, and imports it only when someone actually asks to run it.

Why a static table rather than a `@register` decorator
------------------------------------------------------
A decorator would register each query as its module is imported -- which means enumerating
the names would require importing every module, the exact cost this file exists to avoid.
So the catalogue is a plain dict here: the name and summary are available with no import,
and `load` does the import lazily, once, only for the query being run. One file lists the
queries; the query modules carry no registration boilerplate and do not know they are in a
registry. The small price is that adding a query means adding a line here -- which is a
grep-able, reviewable list, not magic.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chronotrace.query.types import Query


@dataclass(frozen=True, slots=True)
class _Entry:
    """Where a query's class lives (`module:attr`) and what it does, in one line."""

    target: str
    summary: str


_QUERIES: dict[str, _Entry] = {
    "var-writes": _Entry(
        "chronotrace.query.var_writes:VarWritesQuery",
        "every write to a variable, newest context first",
    ),
    "line-hits": _Entry(
        "chronotrace.query.line_hits:LineHitsQuery",
        "every instant a source line executed",
    ),
}


def names() -> list[str]:
    """Every registered query name, sorted. No query module is imported."""
    return sorted(_QUERIES)


def summaries() -> dict[str, str]:
    """`name -> one-line description`, for `--list`. Still imports nothing."""
    return {name: entry.summary for name, entry in sorted(_QUERIES.items())}


def load(name: str) -> type[Query]:
    """Import and return the query class registered under `name`.

    Raises:
        KeyError: no query is registered under that name -- the caller (CLI, server) turns
            it into a message that lists the names that *are* registered.
    """
    entry = _QUERIES[name]
    module_name, attr = entry.target.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)  # type: ignore[no-any-return]
