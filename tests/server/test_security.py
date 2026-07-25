"""The three defences that matter because localhost is not a security boundary.

A page on any origin can reach `127.0.0.1`, so binding loopback protects nothing on its own.
These prove the three things that do: a session id cannot escape the recordings directory
(path containment), a DNS-rebinding request is refused on its Host header, and a cross-origin
page cannot read a response (CORS). Each is a real attack a local dev tool is exposed to, and
each is the kind of defence portfolio projects skip.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chronotrace.server.deps import SessionStore
from chronotrace.server.errors import ProblemError

# -- path containment: an id can never escape the recordings directory ------------------


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        "../secret",
        "../../etc/passwd",
        "/etc/passwd",
        "foo/bar",
        "foo\\bar",
        "",
        ".",
        "a\x00b",
    ],
)
def test_traversing_ids_are_rejected(recordings: Path, session_id: str, bad: str) -> None:
    store = SessionStore(recordings)
    with pytest.raises(ProblemError) as raised:
        store.resolve(bad)
    assert raised.value.code.value == "not_found"


def test_a_legitimate_id_resolves_inside_the_directory(recordings: Path, session_id: str) -> None:
    store = SessionStore(recordings)
    resolved = store.resolve(session_id)
    assert resolved.name == f"{session_id}.chrono"
    assert resolved.parent == recordings.resolve()


def test_url_encoded_traversal_is_404(client: TestClient) -> None:
    # `%2e%2e%2f` is `../` -- decoded into one path param, it must still be rejected, not
    # routed to a file outside the directory.
    for encoded in ("%2e%2e%2fsecret", "%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
        response = client.get(f"/api/sessions/{encoded}/state?seq=0")
        assert response.status_code == 404, encoded


# -- DNS-rebinding: a wrong Host header is refused --------------------------------------


def test_a_rebinding_host_header_is_refused(client: TestClient) -> None:
    # The rebinding attack: evil.com resolves to 127.0.0.1, the browser fetches it, the
    # request carries `Host: evil.com` -- not in the allowlist, so it never reaches a route.
    response = client.get("/api/sessions", headers={"Host": "evil.attacker.example"})
    assert response.status_code == 400


def test_an_allowed_host_is_accepted(client: TestClient) -> None:
    response = client.get("/api/sessions", headers={"Host": "localhost"})
    assert response.status_code == 200


# -- CORS: only the configured UI origin may read a response ----------------------------


def test_the_configured_ui_origin_is_allowed(app_factory: Callable[..., FastAPI]) -> None:
    app = app_factory(allowed_origins=("http://good-ui.local",))
    with TestClient(app) as client:
        response = client.get("/api/sessions", headers={"Origin": "http://good-ui.local"})
        assert response.headers.get("access-control-allow-origin") == "http://good-ui.local"


def test_a_disallowed_origin_gets_no_cors_grant(app_factory: Callable[..., FastAPI]) -> None:
    app = app_factory(allowed_origins=("http://good-ui.local",))
    with TestClient(app) as client:
        response = client.get("/api/sessions", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in response.headers


def test_the_default_denies_every_cross_origin_read(client: TestClient) -> None:
    # No UI origin configured -> deny by default -> no Access-Control-Allow-Origin ever.
    response = client.get("/api/sessions", headers={"Origin": "http://anything.local"})
    assert "access-control-allow-origin" not in response.headers
