"""Small HTTP helpers the routes share: the wire encoder, the cache headers, seq validation.

Kept out of the route modules so the JSON shape, the caching policy and the seq-range rule
each live in one place -- three things that must be identical across every endpoint that
touches them, and three ways for endpoints to drift if each rolled its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Response
from fastapi.responses import JSONResponse

from chronotrace.server import dto
from chronotrace.server.errors import ProblemError

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request

    from chronotrace.store import ChronoReader

NO_STORE = {"Cache-Control": "no-store"}
"""For the two endpoints ADR-0010 does not cache: the session list (the directory changes)
and `POST /query` (a request body, and a query is cheap to re-run)."""


def json_dto(obj: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    """A DTO (or list of DTOs) as its canonical wire bytes.

    Serialised with `dto.to_wire`, the framework-free encoder the day-32 golden tests lock,
    rather than FastAPI's -- so the bytes on the wire are exactly the contract, not whatever
    the transport happens to produce.
    """
    return JSONResponse(content=dto.to_wire(obj), headers=headers)


def cache_headers(etag: str) -> dict[str, str]:
    """Immutable-cache headers for a finished recording's response.

    A finished `.chrono` never changes (day-11 immutability), so a given `seq`'s state is
    the same forever: a strong `ETag` plus `immutable` means the browser never revalidates,
    and there is no invalidation logic to get wrong because nothing is ever invalidated.

    ponytail: only the immutable case exists today. A still-recording session (the day-34
    live stream) will send `Cache-Control: no-store` instead; that branch lands with it.
    """
    return {"ETag": etag, "Cache-Control": "public, max-age=31536000, immutable"}


def cached(request: Request, etag: str, build: Callable[[], Any]) -> Response:
    """A cacheable read: a `304` if the client's ETag still matches, else the built DTO.

    The build thunk runs only on a miss, so a matching `If-None-Match` costs no reconstruct
    or index read. Every immutable read endpoint routes through here so the ETag and the
    `Cache-Control` are stamped one way, not six.
    """
    headers = cache_headers(etag)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return json_dto(build(), headers=headers)


def validate_seq(reader: ChronoReader, seq: int) -> None:
    """Reject a `seq` outside the recording before any layer tries to reconstruct it.

    A `seq` past the end of a *truncated* recording is `TRUNCATED_SEQ` (it may have existed
    before the crash lost the tail), distinct from `SEQ_OUT_OF_RANGE` on an intact one -- the
    UI must not tell a user "no such instant" when the honest answer is "the crash ate it".
    """
    count = len(reader)
    if 0 <= seq < count:
        return
    if reader.truncated:
        raise ProblemError(
            dto.ErrorCode.TRUNCATED_SEQ,
            f"seq {seq} is past the end of a crash-truncated recording (recovered {count} events)",
        )
    raise ProblemError(dto.ErrorCode.SEQ_OUT_OF_RANGE, f"seq {seq} is outside [0, {count})")
