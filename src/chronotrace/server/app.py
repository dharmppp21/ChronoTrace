"""The application factory and the lifespan that owns every open recording.

Why a factory, not a module-level `app`
---------------------------------------
`create_app(config)` builds a fresh application each call, and that is the whole point:
tests build one per test with their own recordings directory and never share state, and there
are no import-time side effects -- a module-level `app = FastAPI()` that opened files on
import would be untestable, would break `uvicorn --reload`, and would do I/O just because
someone imported the module. The resources open in the *lifespan* instead, so they are tied
to the app's run, not to importing this file.

Why the lifespan owns the resources
------------------------------------
The lifespan creates one `SessionStore` and closes it on shutdown. The store opens a
recording once (a mmap and a SQLite connection) and reuses it across every request for that
session -- opening per request would leak a handle on every scrub and, on Windows, block the
index's own rebuild (issue #10). "Open once, close once" is the query engine's resource rule
(`QueryContext`); the server just gives it a process-shaped lifetime.

Localhost is NOT a security boundary
------------------------------------
The server binds `127.0.0.1` and has no auth, because a recording is a program's memory --
its secrets -- and there is no network to authenticate against. But binding localhost does
not make the server safe from the *web*: any page the user visits can send requests to
`127.0.0.1`, so a malicious site could read their recordings through this API. Two specific
defences, both easy to miss and both here:

* **Host-header validation** (`TrustedHostMiddleware`) stops DNS rebinding: an attacker points
  `evil.com` at `127.0.0.1` and gets the browser to fetch `http://evil.com:8000/...`; the
  request carries `Host: evil.com`, which is not in the allowlist, so it is refused before it
  reaches a route.
* **CORS locked to the UI origin** stops a cross-origin page from *reading* a response even if
  it can send the request.

Path containment (in `deps.py`) is the third leg: a session id can never escape the
recordings directory. None of the three would matter if localhost were a boundary; it is not,
so all three are here.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from chronotrace.server import dto
from chronotrace.server.deps import SessionStore
from chronotrace.server.errors import install_error_handlers
from chronotrace.server.routes import router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """What the server needs to run: where the recordings are, and who may talk to it.

    `allowed_hosts` and `allowed_origins` are the DNS-rebinding and CORS locks (see the
    module docstring). The host default is loopback only; the origin default is empty, which
    means "no cross-origin reads at all" until a UI origin is named -- deny by default.
    """

    recordings_dir: Path
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_origins: tuple[str, ...] = ()


def create_app(config: ServerConfig) -> FastAPI:
    """Build a fresh app over `config`. No globals, no import-time I/O -- see the docstring."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = SessionStore(config.recordings_dir)
        app.state.store = store
        # The WebSocket stream validates Origin by hand (CORS does not cover WS), so it needs
        # the allowlist on app.state -- middleware config is not readable from a handler.
        app.state.allowed_origins = config.allowed_origins
        try:
            yield
        finally:
            store.close()  # release every mmap and connection on shutdown

    app = FastAPI(
        title="ChronoTrace API",
        version=dto.WIRE_VERSION,
        summary="Scrub a recording's timeline: state, source, call tree, queries.",
        lifespan=lifespan,
    )
    if config.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.allowed_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )
    # Added last so it is the outermost middleware: a rebinding request is refused on its Host
    # header before CORS or any route runs.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))
    install_error_handlers(app)
    app.include_router(router)
    return app
