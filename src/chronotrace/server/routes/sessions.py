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

    The text comes from disk (the recording stores only a source *hash*, format 1.7). The
    current file is always served when readable; `available` says whether the heatmap aligns to
    it -- False when the file changed since recording -- so the UI can show the code and simply
    withhold the overlay rather than paint the heatmap over a different program.
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
    """The file's current lines, and whether the heatmap aligns to them -- read once, hashed.

    The text is the file on disk *now*; the recording stores only a hash of it (format 1.7),
    not the text. `available` is True only when that hash is present and still matches -- then
    the heatmap's line numbers line up with these lines. On a mismatch (the file changed since
    recording) or no hash (`exec`'d `<string>`, an unreadable file at record time) the lines
    are still returned, so the UI can show the current file, but `available` is False so it does
    not overlay a heatmap aligned to *different* code -- a wrong line is worse than no line. A
    file gone since recording yields no lines at all.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return [], False  # the file is gone since recording -- nothing to show
    available = recorded_hash is not None and hashlib.sha256(raw).hexdigest() == recorded_hash
    return raw.decode("utf-8", "replace").splitlines(), available
