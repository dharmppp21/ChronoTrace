"""Every endpoint against a real recording, plus the error table, caching and cancellation.

A contract test per endpoint (does the screen's data come back in the DTO shape?), the day-13
error hierarchy mapped to statuses as a table, ETag 304s, and the proof that a disconnected
client stops the reconstruction. The one thing deferred to `benchmarks/` is the 1M-event
latency scale; the `/state` p95 is smoke-checked here on a small recording against the ADR
budget, and the real-scale number is the harness's job.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from fastapi.testclient import TestClient

from chronotrace.query import QueryContext
from chronotrace.server import present
from chronotrace.server.deps import Metrics
from chronotrace.server.routes.sessions import _read_source
from chronotrace.server.routes.state import _reconstruct


def _meta(client: TestClient, sid: str) -> dict[str, Any]:
    response = client.get(f"/api/sessions/{sid}")
    assert response.status_code == 200
    return cast("dict[str, Any]", response.json())


def _mid_seq(client: TestClient, sid: str) -> int:
    return int(_meta(client, sid)["event_count"]) // 2


# -- the deferred day-32 test: the spec generates and is well-formed --------------------


def test_openapi_generates_and_is_valid(client: TestClient) -> None:
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    body = spec.json()
    assert body["openapi"].startswith("3.")
    paths = body["paths"]
    for path in (
        "/api/sessions",
        "/api/sessions/{session_id}",
        "/api/sessions/{session_id}/timeline",
        "/api/sessions/{session_id}/state",
        "/api/sessions/{session_id}/source",
        "/api/sessions/{session_id}/calltree",
        "/api/sessions/{session_id}/value",
        "/api/sessions/{session_id}/step",
        "/api/sessions/{session_id}/query",
    ):
        assert path in paths, path


# -- one contract test per endpoint -----------------------------------------------------


def test_list_sessions(client: TestClient, session_id: str) -> None:
    rows = client.get("/api/sessions").json()
    ids = {row["id"] for row in rows}
    assert session_id in ids
    row = next(r for r in rows if r["id"] == session_id)
    assert row["event_count"] > 0
    assert isinstance(row["truncated"], bool)


def test_session_meta(client: TestClient, session_id: str) -> None:
    meta = _meta(client, session_id)
    assert meta["event_count"] > 0
    assert meta["format_version"][0].isdigit()  # "1.7"-ish
    assert meta["keyframe_count"] >= 0


def test_state_reconstructs_and_carries_an_etag(client: TestClient, session_id: str) -> None:
    seq = _mid_seq(client, session_id)
    response = client.get(f"/api/sessions/{session_id}/state", params={"seq": seq})
    assert response.status_code == 200
    body = response.json()
    assert body["seq"] == seq
    assert isinstance(body["frames"], list)
    assert response.headers["etag"]
    assert "immutable" in response.headers["cache-control"]


def test_state_repeat_with_matching_etag_is_304(client: TestClient, session_id: str) -> None:
    seq = _mid_seq(client, session_id)
    first = client.get(f"/api/sessions/{session_id}/state", params={"seq": seq})
    etag = first.headers["etag"]
    again = client.get(
        f"/api/sessions/{session_id}/state",
        params={"seq": seq},
        headers={"If-None-Match": etag},
    )
    assert again.status_code == 304


def test_value_expands_a_variable_ref(client: TestClient, session_id: str) -> None:
    state = client.get(
        f"/api/sessions/{session_id}/state", params={"seq": _mid_seq(client, session_id)}
    ).json()
    refs = [v["ref"] for f in state["frames"] for v in f["variables"] if v["ref"] is not None]
    assert refs, "the recording should have at least one resolvable local"
    value = client.get(f"/api/sessions/{session_id}/value", params={"ref": refs[0]})
    assert value.status_code == 200
    assert isinstance(value.json()["preview"], str)


def test_timeline(client: TestClient, session_id: str) -> None:
    timeline = client.get(f"/api/sessions/{session_id}/timeline", params={"buckets": 8}).json()
    assert timeline["total_events"] > 0
    assert len(timeline["buckets"]) <= 8


def test_source_is_available_and_heatmapped(client: TestClient, session_id: str) -> None:
    source = client.get(f"/api/sessions/{session_id}/source", params={"file": "simple.py"}).json()
    assert source["available"] is True  # the on-disk file still matches the recorded hash
    assert source["lines"]
    assert any(entry["count"] > 0 for entry in source["heatmap"])


def test_source_serves_the_current_file_but_flags_heatmap_alignment(tmp_path: Path) -> None:
    src = tmp_path / "prog.py"
    src.write_bytes(b"a = 1\nb = 2\n")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()

    # hash matches -> lines, and the heatmap aligns
    assert _read_source(str(src), digest) == (["a = 1", "b = 2"], True)

    # the file changed since recording -> lines STILL served (show the current file), but the
    # heatmap must not be overlaid: a wrong line is worse than no line
    lines, available = _read_source(str(src), "0" * 64)
    assert lines == ["a = 1", "b = 2"]
    assert available is False

    # no recorded hash (an exec'd <string>, unreadable at record time) -> lines, no alignment
    assert _read_source(str(src), None) == (["a = 1", "b = 2"], False)

    # the file is gone since recording -> nothing to show
    assert _read_source(str(tmp_path / "missing.py"), digest) == ([], False)


def test_diff_reports_variable_changes_from_deltas(client: TestClient, session_id: str) -> None:
    count = _meta(client, session_id)["event_count"]
    kinds: set[str] = set()
    total_mods: list[dict[str, Any]] = []
    for seq in range(count):
        body = client.get(f"/api/sessions/{session_id}/diff", params={"seq": seq}).json()
        assert body["seq"] == seq
        for change in body["changes"]:
            kinds.add(change["kind"])
            if change["name"] == "total" and change["kind"] == "modified":
                total_mods.append(change)
    assert "added" in kinds  # `total = 0` (and other locals) are creations
    assert "modified" in kinds  # `total += quadruple(i)` changes it
    assert total_mods, "the loop's accumulation of `total` must show as a modification"
    assert all(c["old"] is not None and c["new"] is not None for c in total_mods)
    assert all(c["old"] != c["new"] for c in total_mods)  # a real change, old -> new


def test_obj_id_surfaces_object_identity_only_where_it_exists() -> None:
    assert present._obj_id({"$": "obj", "type": "Foo", "id": 7}) == 7  # a custom object: badge-able
    assert present._obj_id({"$": "list", "items": []}) is None  # dict/list carry no id (#9)
    assert present._obj_id(42) is None  # an atom is not an object


def test_calltree(client: TestClient, session_id: str) -> None:
    tree = client.get(
        f"/api/sessions/{session_id}/calltree", params={"seq": _mid_seq(client, session_id)}
    ).json()
    assert isinstance(tree["frames"], list)


def test_step_forward_lands_on_an_instant_or_edge(client: TestClient, session_id: str) -> None:
    result = client.get(
        f"/api/sessions/{session_id}/step",
        params={"seq": 0, "dir": "forward", "mode": "into"},
    )
    assert result.status_code == 200
    body = result.json()
    assert (body["seq"] is None) != (body["edge"] is None)  # exactly one is set


def test_query_line_hits(client: TestClient, session_id: str) -> None:
    result = client.post(
        f"/api/sessions/{session_id}/query",
        json={"name": "line-hits", "args": {"file": "simple.py", "lineno": 18}},
    )
    assert result.status_code == 200
    body = result.json()
    assert body["hits"], "line 18 (result = n * 2) runs every time double() is called"
    assert all("seq" in hit for hit in body["hits"])
    assert result.headers["cache-control"] == "no-store"


# -- the container-expansion path, without depending on an example's locals --------------


def test_value_expansion_walks_one_level_of_a_container() -> None:
    captured = {
        "$": "list",
        "items": [1, "two", {"$": "dict", "items": [["k", "v"]]}],
        "len": 3,
    }
    value = present.value(captured)
    assert value.kind == "list"
    assert value.children is not None
    assert len(value.children) == 3
    assert value.children[2].has_children is True  # the nested dict


# -- the error table: day-13 hierarchy + request errors -> distinct codes ----------------


def test_unknown_session_is_404_not_found(client: TestClient) -> None:
    response = client.get("/api/sessions/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.headers["content-type"].startswith("application/problem+json")


def test_seq_out_of_range_is_404(client: TestClient, session_id: str) -> None:
    response = client.get(f"/api/sessions/{session_id}/state", params={"seq": 10**9})
    assert response.status_code == 404
    assert response.json()["code"] == "seq_out_of_range"


def test_unknown_query_name_is_400(client: TestClient, session_id: str) -> None:
    response = client.post(
        f"/api/sessions/{session_id}/query", json={"name": "no-such-query", "args": {}}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_query"


def test_unknown_variable_is_404(client: TestClient, session_id: str) -> None:
    response = client.post(
        f"/api/sessions/{session_id}/query",
        json={"name": "var-writes", "args": {"name": "definitely_not_a_variable_zzz"}},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_bad_step_mode_is_400_bad_request(client: TestClient, session_id: str) -> None:
    response = client.get(f"/api/sessions/{session_id}/step", params={"seq": 0, "mode": "sideways"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_corrupt_recording_is_422(client: TestClient, recordings: Path) -> None:
    (recordings / "bad.chrono").write_bytes(b"this is not a chrono file at all")
    response = client.get("/api/sessions/bad")
    assert response.status_code == 422
    assert response.json()["code"] == "corrupt"


def test_problem_body_has_the_full_shape(client: TestClient) -> None:
    problem = client.get("/api/sessions/nope").json()
    assert set(problem) >= {"code", "status", "title", "detail"}
    assert problem["status"] == 404


# -- cancellation: a disconnected client does no reconstruction --------------------------


class _StubRequest:
    """The one thing `_reconstruct` asks of a request: whether the client is gone."""

    def __init__(self, *, disconnected: bool) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


def test_disconnected_client_stops_reconstruction(recordings: Path, session_id: str) -> None:
    async def run() -> None:
        with QueryContext.open(recordings / f"{session_id}.chrono") as ctx:
            metrics = Metrics()
            gone = cast("Request", _StubRequest(disconnected=True))
            assert await _reconstruct(gone, ctx, 0, metrics) is None
            assert metrics.reconstructions == 0  # the whole point: no wasted work
            assert metrics.cancelled == 1

            here = cast("Request", _StubRequest(disconnected=False))
            assert await _reconstruct(here, ctx, 0, metrics) is not None
            assert metrics.reconstructions == 1

    asyncio.run(run())


# -- latency smoke: /state p95 within the ADR-0010 budget on a small recording -----------


def test_state_p95_is_within_budget(client: TestClient, session_id: str) -> None:
    count = _meta(client, session_id)["event_count"]
    client.get(f"/api/sessions/{session_id}/state", params={"seq": 0})  # warm: builds the index
    samples: list[float] = []
    for i in range(40):
        seq = i % count
        start = time.perf_counter()
        response = client.get(f"/api/sessions/{session_id}/state", params={"seq": seq})
        samples.append(time.perf_counter() - start)
        assert response.status_code == 200
    samples.sort()
    p95 = samples[int(0.95 * (len(samples) - 1))]
    assert p95 < 0.050, f"/state p95 {p95 * 1000:.1f} ms exceeds the 50 ms ADR-0010 budget"
