"""Turns the index into **answers**: "who wrote `total`?", "when did line 47 run?".

This is ChronoTrace's public intellectual surface -- the layer that makes the difference
between a recording *viewer* and a debugger you can *interrogate*. A viewer shows you a
state; a query engine lets you ask the timeline a question and get back the instants that
answer it, each one jumpable. The thesis, stated once in `types.py` and worth repeating
here: **a query result is a set of instants you can jump to.**

Public surface
--------------
`Query` (the protocol every query satisfies), `QueryContext` (the injected resources),
`QueryResult`/`Hit`/`Cursor` (the answer and how to page through it), the two shipped
queries, and the `registry` (enumerate and load queries by name without importing them).

What this layer must never import
---------------------------------
`server`, the frontend. It answers questions in terms of `seq`; rendering an answer as a
clickable link belongs above it. It reads `index`, `reconstruct` and `store` -- the arrow
points down.

Design: today ships the typed API and two foundational queries; the causal queries land
day 29. The deliberate *absence* of a query DSL, and the single trigger that would justify
one, are argued in `types.py` and tracked as issue #13.
"""

from chronotrace.query import registry
from chronotrace.query.call_tree import CallersOfQuery, CallTreeQuery
from chronotrace.query.exception_origin import ExceptionOriginQuery
from chronotrace.query.last_write import LastWriteBeforeQuery
from chronotrace.query.line_hits import LineHitsQuery
from chronotrace.query.provenance import ValueProvenanceQuery
from chronotrace.query.types import (
    PAGE_SIZE,
    Cursor,
    Hit,
    Query,
    QueryContext,
    QueryError,
    QueryResult,
    UnknownFile,
    UnknownFunction,
    UnknownName,
)
from chronotrace.query.var_writes import VarWritesQuery

__all__ = [
    "PAGE_SIZE",
    "CallTreeQuery",
    "CallersOfQuery",
    "Cursor",
    "ExceptionOriginQuery",
    "Hit",
    "LastWriteBeforeQuery",
    "LineHitsQuery",
    "Query",
    "QueryContext",
    "QueryError",
    "QueryResult",
    "UnknownFile",
    "UnknownFunction",
    "UnknownName",
    "ValueProvenanceQuery",
    "VarWritesQuery",
    "registry",
]
