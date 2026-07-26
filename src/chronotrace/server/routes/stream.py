"""The live event stream: `WS /api/sessions/{id}/stream`.

Tails the session's `.chrono` while it is being written and pushes one aggregated frame per
poll, then a final frame when the recording completes or is truncated. The heavy lifting is
elsewhere: the tailer (`store.tailer`) turns a growing file into events, and `streaming.frame`
turns a batch into a bounded DTO. This module is the socket loop and the security gate.

Why the Origin header is validated here, by hand
------------------------------------------------
Browsers do **not** apply CORS to WebSocket handshakes -- there is no preflight, and a page on
any origin may open `ws://127.0.0.1:8000/...`. Developers routinely assume the CORS lock that
guards their REST endpoints also guards their sockets; it does not, and that is the hole a
malicious page walks through to read a recording (the debugged program's memory). So this
endpoint checks the `Origin` header explicitly against the same allowlist CORS uses, and
refuses a handshake from anywhere else.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from chronotrace.server import dto, streaming
from chronotrace.server.errors import ProblemError
from chronotrace.store.tailer import ChronoTailer, TailState

if TYPE_CHECKING:
    from collections.abc import Sequence

router = APIRouter()

_POLICY_VIOLATION = 1008  # RFC 6455 close code: the handshake violated policy (bad origin/id)


@router.websocket("/api/sessions/{session_id}/stream")
async def stream(websocket: WebSocket, session_id: str) -> None:
    """Stream the recording's timeline as it fills, then signal it is scrubbable."""
    store = websocket.app.state.store
    if not _origin_ok(websocket, websocket.app.state.allowed_origins):
        await websocket.close(code=_POLICY_VIOLATION)
        return
    try:
        path = store.resolve(session_id)  # path containment; NOT_FOUND -> refuse the handshake
    except ProblemError:
        await websocket.close(code=_POLICY_VIOLATION)
        return
    await websocket.accept()
    await _pump(websocket, ChronoTailer(path))


async def _pump(websocket: WebSocket, tailer: ChronoTailer) -> None:
    """Poll -> aggregate -> send, until the recording ends or the client leaves.

    A frame is sent only when there is something to say (new events, or a state change), so a
    quiet program costs no traffic. If the file stops growing with no footer for `STALE_TIMEOUT`,
    the writer is presumed dead and the recording finalised truncated -- the crash case, from
    the reader's side.
    """
    last_growth = time.monotonic()
    try:
        while True:
            events = tailer.poll()
            now = time.monotonic()
            if events:
                last_growth = now
            elif tailer.state is TailState.RUNNING and now - last_growth > streaming.STALE_TIMEOUT:
                tailer.mark_dead()  # no growth, no footer: the writer is gone
            if events or tailer.state is not TailState.RUNNING:
                payload = streaming.frame(events, total=tailer.total, state=tailer.state)
                await websocket.send_json(dto.to_wire(payload))
            if tailer.state is not TailState.RUNNING:
                return
            await asyncio.sleep(streaming.POLL_INTERVAL)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        return  # client left (RuntimeError: a send after the peer closed) -- nothing to clean up


def _origin_ok(websocket: WebSocket, allowed: Sequence[str]) -> bool:
    """Validate the WebSocket `Origin`. See the module docstring on why CORS does not do this.

    No `Origin` means a non-browser client (a script, a test) -- allowed, because it is not
    subject to the same-origin policy and the `Host` check already gates it. An `Origin` that is
    present must be in the configured UI allowlist; a page on any other origin is refused.
    """
    origin = websocket.headers.get("origin")
    return origin is None or origin in allowed
