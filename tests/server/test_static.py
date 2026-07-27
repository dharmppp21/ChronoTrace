"""Serving the built frontend in production: it mounts at `/`, and never shadows the API.

The two things that must hold: a bundled UI is reachable at `/`, and mounting a catch-all at
`/` does not swallow `/api/...` or `/openapi.json`; and a checkout with no built UI is an
API-only server, not a crash -- because dev serves the UI from Vite, and only a release wheel
carries the assets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronotrace.server.app import ServerConfig, create_app

_HOSTS = ("testserver", "localhost", "127.0.0.1")


def test_a_built_ui_is_served_and_does_not_shadow_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recordings: Path
) -> None:
    ui = tmp_path / "_ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>ChronoTrace UI</title>", encoding="utf-8")
    monkeypatch.setattr("chronotrace.server.app._UI_DIR", ui)

    app = create_app(ServerConfig(recordings_dir=recordings, allowed_hosts=_HOSTS))
    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "ChronoTrace UI" in root.text  # the SPA index, served from the package
        assert client.get("/api/sessions").status_code == 200  # the mount does not eat the API
        assert client.get("/openapi.json").status_code == 200  # nor the generated spec


def test_no_built_ui_is_an_api_only_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recordings: Path
) -> None:
    monkeypatch.setattr("chronotrace.server.app._UI_DIR", tmp_path / "never_built")
    app = create_app(ServerConfig(recordings_dir=recordings, allowed_hosts=_HOSTS))
    with TestClient(app) as client:
        assert client.get("/").status_code == 404  # no UI mounted, no crash
        assert client.get("/api/sessions").status_code == 200  # the API is unaffected
