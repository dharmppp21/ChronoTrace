"""The read endpoints that describe a recording: list, meta, timeline, source, call tree.

Each is thin on purpose -- parse the request, call one layer function, map to a DTO. A route
here holds no logic the CLI could not also want: the density profile, the line heatmap and
the live-frame stack are all `index`/`query` calls, not computation invented at the HTTP
edge. That is the rule ADR-0010 named "endpoints derived from screens, logic in the layers".

Every response but the session list is an immutable read: a finished recording's answer at a
given seq/file never changes, so each carries a strong ETag and caches forever (`cached`). The
list is `no-store` because the directory it reflects can change under it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from chronotrace.index import heatmap, profile, stack_at
from chronotrace.query._resolve import resolve_file
from chronotrace.server import dto, present
from chronotrace.server.deps import SessionStore, get_context, get_store
from chronotrace.server.routes._http import NO_STORE, cached, json_dto, validate_seq

if TYPE_CHECKING:
    from chronotrace.query import QueryContext

router = APIRouter()


@router.get("/api/sessions", response_model=list[dto.SessionSummary])
def list_sessions(store: SessionStore = Depends(get_store)) -> Response:
    """Every readable recording in the directory. `no-store` -- the directory changes."""
    return json_dto(store.summaries(), headers=NO_STORE)


@router.get("/api/sessions/{session_id}", response_model=dto.SessionMeta)
def get_session(
    session_id: str, request: Request, store: SessionStore = Depends(get_store)
) -> Response:
    """One recording's metadata -- cheaply, without building its index."""
    etag = f'"{store.fingerprint(session_id)}-meta"'
    return cached(request, etag, lambda: store.meta(session_id))


@router.get("/api/sessions/{session_id}/timeline", response_model=dto.Timeline)
def get_timeline(
    session_id: str,
    request: Request,
    buckets: int | None = None,
    ctx: QueryContext = Depends(get_context),
    store: SessionStore = Depends(get_store),
) -> Response:
    """The scrubber's density background, downsampled to `buckets` columns if asked."""
    etag = f'"{store.fingerprint(session_id)}-timeline-{buckets}"'
    return cached(
        request,
        etag,
        lambda: present.timeline(profile(ctx.db), len(ctx.reader), ctx.reader.truncated, buckets),
    )


@router.get("/api/sessions/{session_id}/source", response_model=dto.Source)
def get_source(
    session_id: str,
    file: str,
    request: Request,
    ctx: QueryContext = Depends(get_context),
    store: SessionStore = Depends(get_store),
) -> Response:
    """Source text plus its per-line execution heatmap.

    The text comes from disk -- the recording stores only a source *hash* (format 1.7) -- and
    is served only when that hash still matches, so the heatmap is never drawn over lines
    that may be a different program.
    """
    etag = f'"{store.fingerprint(session_id)}-source-{file}"'
    return cached(request, etag, lambda: _source(ctx, file))


@router.get("/api/sessions/{session_id}/calltree", response_model=dto.CallTree)
def get_calltree(
    session_id: str,
    seq: int,
    request: Request,
    ctx: QueryContext = Depends(get_context),
    store: SessionStore = Depends(get_store),
) -> Response:
    """The frames live at `seq`, each with its parent so the UI can nest them."""
    validate_seq(ctx.reader, seq)
    etag = f'"{store.fingerprint(session_id)}-calltree-{seq}"'
    return cached(
        request, etag, lambda: present.call_tree(stack_at(ctx.db, seq), ctx.reader.strings())
    )


def _source(ctx: QueryContext, file: str) -> dto.Source:
    file_id, path = resolve_file(ctx.db, file)  # UnknownFile -> NOT_FOUND via the error handler
    lines, available = _read_source(path, ctx.reader.strings().hash_of(path))
    return present.source(path, lines, heatmap(ctx.db, file_id), available)


def _read_source(path: str, recorded_hash: str | None) -> tuple[list[str], bool]:
    """The file's lines and whether they can be trusted -- read once, hashed, decoded.

    Trusted only when the recording carried a hash for this file *and* the bytes on disk
    still match it. No hash (an `exec`'d `<string>`, an unreadable file at record time) or a
    changed file both read as untrusted, and the lines are withheld so the UI shows a
    placeholder rather than the wrong source.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return [], False
    if recorded_hash is None or hashlib.sha256(raw).hexdigest() != recorded_hash:
        return [], False
    return raw.decode("utf-8", "replace").splitlines(), True
