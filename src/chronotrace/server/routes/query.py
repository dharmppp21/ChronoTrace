"""`POST /query`: dispatch a typed query by name, page its jumpable instants back.

The API's query surface is the same typed one the CLI uses -- a name plus primitive args
through the day-28 registry, never a string DSL (the #13 decision). This route dispatches:
it looks the query class up by name, constructs it from the request's args, runs it, and maps
the page of hits to the wire. The queries themselves live in the `query` layer, so the API
and the CLI share every one of them and neither reimplements a question.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from chronotrace.query import PAGE_SIZE, Cursor, registry
from chronotrace.server import dto, present
from chronotrace.server.deps import get_context
from chronotrace.server.errors import ProblemError
from chronotrace.server.routes._http import NO_STORE, json_dto

if TYPE_CHECKING:
    from fastapi.responses import JSONResponse

    from chronotrace.query import QueryContext

router = APIRouter()


@router.get("/api/queries", response_model=list[dto.QueryDescriptor])
def list_queries() -> JSONResponse:
    """Every query the engine offers, with its argument schema, so the UI builds forms from it.

    Global, not per-session: the catalogue is the same for every recording. The day-28 registry
    makes it enumerable without running anything -- add a query there and it appears here, and in
    the panel, with no frontend change. That decoupling is the endpoint's whole reason to exist.
    """
    return json_dto(present.query_catalog())


@router.post("/api/sessions/{session_id}/query", response_model=dto.QueryResult)
def run_query(body: dto.QueryRequest, ctx: QueryContext = Depends(get_context)) -> JSONResponse:
    """Run one registered query and return a page of instants.

    A query that ran and found nothing is an empty page, not an error. An *unknown name* is
    UNKNOWN_QUERY, malformed *args* are BAD_REQUEST, and a query that rejects its input
    (`UnknownName`, a variable that was never recorded) bubbles to NOT_FOUND via the error
    handler -- three different mistakes with three different fixes, as the codes promise.
    """
    try:
        query_cls = registry.load(body.name)
    except KeyError as exc:
        known = ", ".join(registry.names())
        raise ProblemError(
            dto.ErrorCode.UNKNOWN_QUERY, f"no query named {body.name!r}; known: {known}"
        ) from exc
    try:
        query = query_cls(**body.args)
    except TypeError as exc:
        raise ProblemError(
            dto.ErrorCode.BAD_REQUEST, f"bad args for query {body.name!r}: {exc}"
        ) from exc
    cursor = Cursor(body.cursor) if body.cursor is not None else None
    result = query.execute(ctx, cursor, limit=body.limit or PAGE_SIZE)
    return json_dto(present.query_result(result), headers=NO_STORE)
