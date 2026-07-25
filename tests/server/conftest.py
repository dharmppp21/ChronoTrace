"""A server over a real recording: record `examples/simple.py`, serve it, drive it with a client.

The whole stack, not a mock -- record -> `.chrono` on disk -> `create_app` -> `TestClient` --
so a passing route test is a fact about the product, the same discipline the query fixtures
keep. `app_factory` lets the security tests build apps with custom host/origin allowlists.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chronotrace.cli import intern_tables
from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.scope import Scope
from chronotrace.server.app import ServerConfig, create_app
from chronotrace.store import ChronoWriter

EXAMPLES = Path(__file__).parent.parent.parent / "examples"

# The TestClient sends `Host: testserver`; it must be allowed for functional tests to reach a
# route at all. Kept out of the production defaults (localhost only) -- tests opt in here.
ALLOWED_HOSTS = ("testserver", "localhost", "127.0.0.1")


def record_into(recordings: Path, module: str = "simple", func: str = "main") -> str:
    """Record `examples/<module>.py::<func>` into `recordings`, returning its session id."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        entry = getattr(importlib.import_module(module), func)
    finally:
        sys.path.remove(str(EXAMPLES))
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(roots=[str(EXAMPLES)]))
    with recorder:
        entry()
    sid = f"{module.replace('.', '_')}_{func}"
    with (recordings / f"{sid}.chrono").open("wb") as handle:
        writer = ChronoWriter(handle)
        writer.add_strings(intern_tables(recorder))
        for captured in recorder.values:
            writer.add_value(captured)
        for event in sink.events:
            writer.add(event)
        writer.close()
    return sid


@pytest.fixture
def recordings(tmp_path: Path) -> Path:
    """An empty recordings directory the server serves."""
    directory = tmp_path / "recordings"
    directory.mkdir()
    return directory


@pytest.fixture
def session_id(recordings: Path) -> str:
    """`examples/simple.py`, recorded into the directory. The default session under test."""
    return record_into(recordings)


@pytest.fixture
def app_factory(recordings: Path) -> Callable[..., FastAPI]:
    """Build an app over the recordings directory, with overridable host/origin allowlists."""

    def make(
        allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS,
        allowed_origins: tuple[str, ...] = (),
    ) -> FastAPI:
        return create_app(
            ServerConfig(
                recordings_dir=recordings,
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            )
        )

    return make


@pytest.fixture
def client(app_factory: Callable[..., FastAPI], session_id: str) -> Iterator[TestClient]:
    """A client over the default app. `with` runs the lifespan, so the store opens and closes."""
    with TestClient(app_factory()) as test_client:
        yield test_client
