"""The one place an exception becomes an HTTP problem-details body.

Problem this solves: every layer below raises *typed* failures -- `CorruptRecording`,
`UnknownName`, a `seq` out of range -- and each means a different thing the user should do
about it. A route that translated each at its raise site would spread the status table
across a dozen files and let two of them disagree. So the mapping lives here, once, and the
routes raise (or let bubble) typed exceptions and never touch a status code.

Interface: `ProblemError` (what a route raises for a domain failure it detects itself),
`code_for` (engine exception -> `ErrorCode`), and `install_error_handlers` (wires the three
handlers into an app).

It must never know: what any endpoint does. It knows exception types and the day-32 status
table (`dto.STATUS_FOR`), nothing else.

Why problem-details and not a bare status: a `404` alone cannot tell a client whether the
*session* is missing or the *seq* is past the end -- one is "pick another recording", the
other "pick another instant". The `ErrorCode` in the body is the machine-readable reason a
client branches on, exactly as `dto.py` argued when it defined the codes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from chronotrace.query import QueryError
from chronotrace.reconstruct import MissingValue
from chronotrace.server import dto
from chronotrace.store.errors import (
    ChronoError,
    CorruptRecording,
    TruncatedRecording,
    UnsupportedVersion,
)

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.responses import Response


class ProblemError(Exception):
    """A failure a route detected itself, carrying the `ErrorCode` it should become.

    Raised for the failures that are not an engine exception but a decision this layer made
    -- a session id that does not resolve (NOT_FOUND), a `seq` outside the recording
    (SEQ_OUT_OF_RANGE), an unknown query name (UNKNOWN_QUERY). The handler turns it into the
    canonical problem-details body, so a route never builds a `Response` by hand.
    """

    def __init__(self, code: dto.ErrorCode, detail: str, instance: str | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.instance = instance


# Ordered specific-before-base: an exception is matched against the first type it is an
# instance of. `MissingValue`, `TruncatedRecording` and `UnsupportedVersion` are all
# `ChronoError` subclasses, so the catch-all `ChronoError` row must come last.
_ENGINE_MAPPING: tuple[tuple[type[BaseException], dto.ErrorCode], ...] = (
    (UnsupportedVersion, dto.ErrorCode.UNSUPPORTED_VERSION),
    (MissingValue, dto.ErrorCode.CORRUPT),
    (TruncatedRecording, dto.ErrorCode.CORRUPT),
    (CorruptRecording, dto.ErrorCode.CORRUPT),
    (ChronoError, dto.ErrorCode.CORRUPT),  # any other unreadable recording
    (QueryError, dto.ErrorCode.NOT_FOUND),  # a name (var/file/function) not in the recording
)


def code_for(exc: BaseException) -> dto.ErrorCode | None:
    """The `ErrorCode` for an engine exception, or None if it is not one we map."""
    for exc_type, code in _ENGINE_MAPPING:
        if isinstance(exc, exc_type):
            return code
    return None


def _response(problem: dto.Problem) -> JSONResponse:
    """A problem-details response: the wire-shaped `Problem` at its own status and media type."""
    return JSONResponse(
        status_code=problem.status,
        content=dto.to_wire(problem),
        media_type="application/problem+json",
    )


async def _handle_problem_error(request: Request, exc: Exception) -> Response:
    """A `ProblemError` a route raised -> its declared code."""
    err = cast("ProblemError", exc)
    return _response(dto.problem(err.code, err.detail, err.instance or request.url.path))


async def _handle_engine_error(request: Request, exc: Exception) -> Response:
    """A `ChronoError`/`QueryError` that bubbled out of a route -> its mapped code.

    The safety net: routes convert the failures they can name to a precise `ProblemError`,
    and anything else (a corrupt block reached mid-read, an unknown variable) lands here and
    is mapped by type rather than leaking a 500.
    """
    code = code_for(exc) or dto.ErrorCode.CORRUPT
    return _response(dto.problem(code, str(exc) or code.value, request.url.path))


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    """A malformed query param or request body -> BAD_REQUEST, in the same problem shape.

    FastAPI would answer its own `422` with a differently-shaped body; routing it through
    here keeps every error the API returns a `Problem`, so a client parses one format.
    """
    err = cast("RequestValidationError", exc)
    detail = "; ".join(_one(e) for e in err.errors()) or "the request is malformed"
    return _response(dto.problem(dto.ErrorCode.BAD_REQUEST, detail, request.url.path))


def _one(error: object) -> str:
    """One FastAPI validation error as `loc: msg`, defensively (its shape is a dict)."""
    if not isinstance(error, dict):
        return str(error)
    loc = ".".join(str(part) for part in error.get("loc", ()))
    return f"{loc}: {error.get('msg', 'invalid')}" if loc else str(error.get("msg", "invalid"))


def install_error_handlers(app: FastAPI) -> None:
    """Register the exception handlers.

    Base classes catch their whole subtree (Starlette walks the exception's MRO), so
    `ChronoError` covers every recording failure and `QueryError` every query failure with two
    registrations.
    """
    app.add_exception_handler(ProblemError, _handle_problem_error)
    app.add_exception_handler(ChronoError, _handle_engine_error)
    app.add_exception_handler(QueryError, _handle_engine_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
