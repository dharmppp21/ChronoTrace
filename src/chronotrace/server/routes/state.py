"""The hot path: `/state` for a dragging playhead, plus `/value` expansion and `/step`.

`/state` is the endpoint a drag fires continuously, so its design is where the caching and
cancellation decisions live:

* **ETag + immutable caching.** A finished recording's state at a `seq` never changes, so the
  response carries a strong `ETag` derived from `(recording fingerprint, seq)` and is
  `immutable`. A repeat drag over the same instant is a `304` the browser answers from cache
  without the server reconstructing anything.

* **Cancellation.** During a drag the browser aborts the fetch for every instant the playhead
  has already left, which closes the connection. We check `request.is_disconnected()` before
  doing the expensive reconstruction and skip it if the client is gone -- so the ~90% of
  in-flight requests that are already stale cost nothing, and the one the user is waiting on
  is not stuck behind them. The blocking reconstruction itself runs in a threadpool, so a
  hundred concurrent `/state` requests from one drag do not serialise on the event loop; the
  browser's aborts are what make "the last one wins" true, and we simply honour them.

`/value` and `/step` are ordinary immutable reads and cache the same way, via `cached`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from chronotrace.reconstruct import Direction, step, step_out, step_over
from chronotrace.server import dto, present
from chronotrace.server.deps import Metrics, SessionStore, get_context, get_store
from chronotrace.server.errors import ProblemError
from chronotrace.server.routes._http import cache_headers, cached, json_dto, validate_seq

if TYPE_CHECKING:
    from chronotrace.query import QueryContext
    from chronotrace.reconstruct import StepResult as EngineStep

router = APIRouter()

_DIRECTION = {
    "forward": Direction.FORWARD,
    "fwd": Direction.FORWARD,
    "backward": Direction.BACKWARD,
    "back": Direction.BACKWARD,
}
_MODE = {"into": step, "over": step_over, "out": step_out}


@router.get("/api/sessions/{session_id}/state", response_model=dto.State)
async def get_state(
    session_id: str,
    seq: int,
    request: Request,
    ctx: QueryContext = Depends(get_context),
    store: SessionStore = Depends(get_store),
) -> Response:
    """The reconstructed instant at `seq`, cached hard and cancellable on disconnect."""
    validate_seq(ctx.reader, seq)
    etag = f'"{store.fingerprint(session_id)}-state-{seq}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers(etag))
    state = await _reconstruct(request, ctx, seq, store.metrics)
    if state is None:  # the client left before we did the work
        return Response(status_code=499)
    return json_dto(state, headers=cache_headers(etag))


async def _reconstruct(
    request: Request, ctx: QueryContext, seq: int, metrics: Metrics
) -> dto.State | None:
    """Reconstruct `seq` off the event loop, unless the client has already disconnected.

    Split out so the cancellation contract is unit-testable without a live socket: a stub
    request whose `is_disconnected` is True must leave `metrics.reconstructions` untouched.
    """
    if await request.is_disconnected():
        metrics.cancelled += 1
        return None
    metrics.reconstructions += 1
    return await run_in_threadpool(_build_state, ctx, seq)


def _build_state(ctx: QueryContext, seq: int) -> dto.State:
    return present.state(ctx, ctx.reconstructor.reconstruct(seq))


@router.get("/api/sessions/{session_id}/value", response_model=dto.Value)
def get_value(
    session_id: str,
    ref: int,
    request: Request,
    ctx: QueryContext = Depends(get_context),
    store: SessionStore = Depends(get_store),
) -> Response:
    """One captured value, expanded one level -- the lazy click-to-expand of a variable."""
    etag = f'"{store.fingerprint(session_id)}-value-{ref}"'
    return cached(request, etag, lambda: _value(ctx, ref))


def _value(ctx: QueryContext, ref: int) -> dto.Value:
    """Resolve one value ref.

    A ref with no value in the pool is a bad handle (a client error -> 404), distinct from a
    corrupt recording (422) -- the user acts differently about each.
    """
    from chronotrace.reconstruct import MissingValue

    try:
        captured = ctx.resolver.resolve(ref)
    except MissingValue as exc:
        raise ProblemError(dto.ErrorCode.NOT_FOUND, f"no value at ref {ref}") from exc
    return present.value(captured)


@router.get("/api/sessions/{session_id}/diff", response_model=dto.Diff)
def get_diff(
    session_id: str,
    seq: int,
    request: Request,
    ctx: QueryContext = Depends(get_context),
    store: SessionStore = Depends(get_store),
) -> Response:
    """What changed from `seq-1` to `seq` -- the variable diff, read from the day-16 deltas.

    Immutable per `seq` (a finished recording's transition never changes), so it caches like
    `/state`: a repeat drag over the same instant is a 304.
    """
    validate_seq(ctx.reader, seq)
    etag = f'"{store.fingerprint(session_id)}-diff-{seq}"'
    return cached(request, etag, lambda: present.diff(ctx, seq))


@router.get("/api/sessions/{session_id}/step", response_model=dto.StepResult)
def get_step(
    session_id: str,
    seq: int,
    request: Request,
    direction: str = Query("forward", alias="dir"),
    mode: str = "into",
    ctx: QueryContext = Depends(get_context),
    store: SessionStore = Depends(get_store),
) -> Response:
    """Step from `seq`: `mode` in {into, over, out}, `dir` in {forward, backward}.

    A boundary (start/end of the recording, or a truncated tail) comes back as an `edge`, not
    an error -- it is the value the UI renders as a disabled button.
    """
    validate_seq(ctx.reader, seq)
    etag = f'"{store.fingerprint(session_id)}-step-{seq}-{direction}-{mode}"'
    return cached(request, etag, lambda: _step(ctx, seq, direction, mode))


def _step(ctx: QueryContext, seq: int, direction: str, mode: str) -> dto.StepResult:
    stepper = _MODE.get(mode.lower())
    towards = _DIRECTION.get(direction.lower())
    if stepper is None or towards is None:
        raise ProblemError(
            dto.ErrorCode.BAD_REQUEST,
            f"mode must be one of {sorted(_MODE)}, dir one of {sorted(_DIRECTION)}",
        )
    result: EngineStep = stepper(ctx.reader, seq, towards)
    return present.step_result(result)
