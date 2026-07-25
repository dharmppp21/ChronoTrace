"""Resource ownership and dependency injection: one recording, one open handle, one close.

Problem this solves: the query engine already decided that a recording is *one* mmap and
*one* SQLite connection, opened once and closed once (`QueryContext`). A web server is where
that decision is most easily broken -- open a reader per request and you leak a file handle
on every scrub, and on Windows a lingering handle blocks the index's own rebuild (issue
#10). So the server opens a `QueryContext` per session lazily, caches it for the process's
life, and closes them all on shutdown. The lifespan (in `app.py`) owns this store's
lifetime; this file owns a session's.

Interface: `SessionStore` (the cache and the resolver), `get_store`/`get_context` (the DI
providers a route declares with `Depends`), and `Metrics` (the counters the cancellation
test asserts against).

It must never know: what an endpoint renders. It resolves an id to a `QueryContext` and
enforces that the id cannot escape the recordings directory.

Why the containment check exists even though we bind localhost
--------------------------------------------------------------
`GET /api/sessions/{id}` takes `id` from the URL, and a naive `dir / id` lets `id="../../etc/
passwd"` read any file. Binding `127.0.0.1` does **not** make this safe: a web page on any
origin can issue requests to `127.0.0.1`, so a malicious page could walk the filesystem
through this endpoint. The id is therefore constrained to a bare filename and the resolved
path is re-checked to sit directly in the recordings directory -- resolve first, then verify
containment, because only the resolved path reveals where `..` actually leads.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastapi import Depends, Request

from chronotrace.index.db import sidecar_path
from chronotrace.index.schema import fingerprint, staleness
from chronotrace.query import QueryContext
from chronotrace.server import dto, present
from chronotrace.server.errors import ProblemError
from chronotrace.store import ChronoReader
from chronotrace.store.errors import ChronoError

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(slots=True)
class Metrics:
    """Counters the server exposes for tests and later observability.

    `reconstructions` counts the expensive `/state` work actually performed; `cancelled`
    counts the requests skipped because the client had already disconnected. The point of
    cancellation is that the first number does not grow for work nobody is waiting for.
    """

    reconstructions: int = 0
    cancelled: int = 0


class SessionStore:
    """The process-lifetime cache of open recordings, keyed by session id.

    One `QueryContext` per recording, opened on first use and reused after -- so the day-20
    locality cache and the mmap survive across the requests of a single playhead drag, which
    is what makes dragging cheap. Closed together on shutdown by the lifespan.
    """

    __slots__ = ("_contexts", "_lock", "dir", "metrics")

    def __init__(self, recordings_dir: Path) -> None:
        self.dir = recordings_dir.resolve()
        self._contexts: dict[str, QueryContext] = {}
        # ponytail: one lock for the whole store -- a first-open of session A blocks a
        # first-open of B for the build's duration. Fine for a local single-user tool; make
        # it per-id if concurrent multi-session first-opens ever matter.
        self._lock = threading.Lock()
        self.metrics = Metrics()

    def resolve(self, session_id: str) -> Path:
        """The recording path for `session_id`, or raise NOT_FOUND. Enforces containment.

        Rejects any id that is not a bare filename (a separator, `..`, an absolute path, a
        null byte) *and* re-checks that the resolved path's parent is the recordings
        directory -- the second check is what catches a `..` that the first somehow missed.
        """
        if not _safe_id(session_id):
            raise _no_session(session_id)
        try:
            path = (self.dir / f"{session_id}.chrono").resolve()
        except (ValueError, OSError):  # a null byte or an un-resolvable path on this OS
            raise _no_session(session_id) from None
        if path.parent != self.dir or not path.is_file():
            raise _no_session(session_id)
        return path

    def summaries(self) -> list[dto.SessionSummary]:
        """Every readable recording in the directory. A damaged one is skipped, not fatal."""
        out: list[dto.SessionSummary] = []
        for path in sorted(self.dir.glob("*.chrono")):
            try:
                with ChronoReader.open(path) as reader:
                    indexed = self._indexed(path)
                    out.append(present.session_summary(path.stem, str(path), reader, indexed))
            except ChronoError:
                continue  # a corrupt file in the folder must not break the whole listing
        return out

    def meta(self, session_id: str) -> dto.SessionMeta:
        """Metadata for one recording -- a transient reader, so it never triggers a build."""
        path = self.resolve(session_id)
        with ChronoReader.open(path) as reader:
            return present.session_meta(session_id, str(path), reader, self._indexed(path))

    def context(self, session_id: str) -> QueryContext:
        """The cached `QueryContext` for `session_id`, opening (and lazily indexing) it once.

        A recording deleted since it was cached fails `resolve` and its stale handle is
        dropped -- so the server answers 404 rather than serving a phantom from a dead mmap.
        """
        try:
            path = self.resolve(session_id)
        except ProblemError:
            self._evict(session_id)  # a vanished recording: close any handle we still hold
            raise
        with self._lock:
            cached = self._contexts.get(session_id)
            if cached is not None:
                return cached
            ctx = QueryContext.open(path)  # ADR-0008: builds the index if missing or stale
            self._contexts[session_id] = ctx
            return ctx

    def fingerprint(self, session_id: str) -> str:
        """The recording's cheap content fingerprint -- the ETag's stable, immutable half."""
        return fingerprint(self.resolve(session_id))

    def close(self) -> None:
        """Close every open recording. Called from the lifespan on shutdown."""
        with self._lock:
            for ctx in self._contexts.values():
                with contextlib.suppress(Exception):
                    ctx.close()
            self._contexts.clear()

    def _indexed(self, recording: Path) -> bool:
        """Whether a current index exists for `recording` without building one.

        Opens the sidecar only to read its staleness stamp and closes it immediately -- a
        stale index reads as not-indexed, because it will be discarded and rebuilt on use.
        """
        path = sidecar_path(recording)
        if not path.exists():
            return False
        connection = sqlite3.connect(path)
        try:
            return staleness(connection, recording) is None
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()

    def _evict(self, session_id: str) -> None:
        with self._lock:
            ctx = self._contexts.pop(session_id, None)
        if ctx is not None:
            with contextlib.suppress(Exception):
                ctx.close()


_RESERVED = {".", ".."}


def _no_session(session_id: str) -> ProblemError:
    """The one NOT_FOUND a bad or missing session id becomes -- raised from three checks."""
    return ProblemError(dto.ErrorCode.NOT_FOUND, f"no session named {session_id!r}")


def _safe_id(session_id: str) -> bool:
    """A bare, non-traversing filename: no separator, no `..`, no null byte."""
    return (
        bool(session_id)
        and session_id == Path(session_id).name
        and session_id not in _RESERVED
        and "\x00" not in session_id
    )


def get_store(request: Request) -> SessionStore:
    """The app's session store, placed on `app.state` by the lifespan."""
    return cast("SessionStore", request.app.state.store)


def get_context(
    session_id: str, store: SessionStore = Depends(get_store)
) -> Iterator[QueryContext]:
    """The `QueryContext` for the `{session_id}` path param -- the shared engine resource.

    A `def` dependency, so FastAPI runs it in a threadpool: a first-open that has to build a
    large index does not block the event loop. This is the day-28 injection decision paying
    off across a second consumer -- the CLI and the API construct the exact same context.
    """
    yield store.context(session_id)
