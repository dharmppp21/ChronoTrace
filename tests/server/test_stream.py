"""The WebSocket live stream: it fills, it completes, it drops-and-summarises, it checks Origin.

The Origin test is the one to keep honest: WebSockets bypass CORS in browsers, so a stream that
did not validate `Origin` by hand would be readable by any web page -- the hole this endpoint
exists to close.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from chronotrace.recorder.events import Event, EventKind
from chronotrace.server import streaming
from chronotrace.store import StreamingFileSink
from chronotrace.store.strings import Strings


def _ev(seq: int, *, raise_: bool = False) -> Event:
    kind = EventKind.RAISE if raise_ else EventKind.LINE
    return Event(
        seq=seq,
        kind=kind,
        timestamp_ns=seq,
        thread_id=1,
        frame_id=1,
        code_id=0,
        lineno=seq % 10 + 1,
    )


def _make_complete(path: Path, n: int, *, all_raise: bool = False) -> None:
    """A finished recording of `n` events -- the tailer sees it all in one COMPLETE frame."""
    sink = StreamingFileSink(path, block_events=8, flush_interval=0.0)
    for seq in range(n):
        sink.emit(_ev(seq, raise_=all_raise))
    sink.close()
    sink.finalize([], Strings())


def _emit_over_time(sink: StreamingFileSink, n: int, exc_at: int) -> None:
    for seq in range(n):
        sink.emit(_ev(seq, raise_=(seq == exc_at)))
        time.sleep(0.01)
    sink.close()
    sink.finalize([], Strings())


def test_stream_fills_then_signals_complete(
    app_factory: Callable[..., FastAPI], recordings: Path
) -> None:
    sink = StreamingFileSink(recordings / "live.chrono", block_events=8, flush_interval=0.0)
    writer = threading.Thread(target=_emit_over_time, args=(sink, 40, 20))
    writer.start()
    frames = []
    with (
        TestClient(app_factory()) as client,
        client.websocket_connect("/api/sessions/live/stream") as ws,
    ):
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["state"] != "running":
                break
    writer.join()

    assert frames[-1]["state"] == "complete"
    assert frames[-1]["total_events"] == 40
    counted = sum(d["event_count"] for f in frames for d in f["density"])
    assert counted == 40  # density counts every event -- the timeline shape is complete
    assert any(nt["kind"] == "raise" and nt["seq"] == 20 for f in frames for nt in f["notable"])


def test_backpressure_caps_notable_and_summarises_the_rest(
    app_factory: Callable[..., FastAPI], recordings: Path
) -> None:
    # 200 exceptions arriving in one batch: notable is capped per frame, the overflow becomes a
    # `dropped` count, and density still counts all 200 -- frames drop, data never does.
    _make_complete(recordings / "storm.chrono", 200, all_raise=True)
    shown = dropped = counted = 0
    with (
        TestClient(app_factory()) as client,
        client.websocket_connect("/api/sessions/storm/stream") as ws,
    ):
        while True:
            frame = ws.receive_json()
            assert len(frame["notable"]) <= streaming.MAX_NOTABLE  # bounded per frame
            shown += len(frame["notable"])
            dropped += frame["dropped"]
            counted += sum(d["event_count"] for d in frame["density"])
            if frame["state"] != "running":
                break
    assert counted == 200  # every event in the density -- no data lost from the shape
    assert shown + dropped == 200  # 200 exceptions: some shown, the rest summarised


def test_a_finished_recording_streams_one_complete_frame(
    app_factory: Callable[..., FastAPI], recordings: Path
) -> None:
    _make_complete(recordings / "done.chrono", 8)
    with (
        TestClient(app_factory()) as client,
        client.websocket_connect("/api/sessions/done/stream") as ws,
    ):
        frame = ws.receive_json()
    assert frame["state"] == "complete"
    assert frame["total_events"] == 8


def test_two_clients_share_one_stream(
    app_factory: Callable[..., FastAPI], recordings: Path
) -> None:
    _make_complete(recordings / "shared.chrono", 8)
    with (
        TestClient(app_factory()) as client,
        client.websocket_connect("/api/sessions/shared/stream") as a,
        client.websocket_connect("/api/sessions/shared/stream") as b,
    ):
        assert a.receive_json()["total_events"] == 8
        assert b.receive_json()["total_events"] == 8


def test_a_foreign_origin_is_refused(app_factory: Callable[..., FastAPI], recordings: Path) -> None:
    _make_complete(recordings / "s.chrono", 4)  # the session exists: it is the ORIGIN we refuse
    app = app_factory(allowed_origins=("http://good-ui.local",))
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/api/sessions/s/stream", headers={"Origin": "http://evil.example"}
        ) as ws,
    ):
        ws.receive_json()


def test_the_configured_origin_is_allowed(
    app_factory: Callable[..., FastAPI], recordings: Path
) -> None:
    _make_complete(recordings / "s.chrono", 4)
    app = app_factory(allowed_origins=("http://good-ui.local",))
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/sessions/s/stream", headers={"Origin": "http://good-ui.local"}
        ) as ws,
    ):
        assert ws.receive_json()["state"] == "complete"


def test_an_unknown_session_stream_is_refused(app_factory: Callable[..., FastAPI]) -> None:
    with (
        TestClient(app_factory()) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/sessions/nope/stream") as ws,
    ):
        ws.receive_json()
