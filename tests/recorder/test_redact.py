"""Redaction: the secret value is never read, and the omission is visible.

The load-bearing test is `test_secret_value_never_reaches_capture`: it spies on
`capture` and proves the secret was never passed to it. "Capture then scrub"
would fail this test, which is the point -- the secret must never enter our
buffers, so redaction gates the read, not the write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chronotrace.recorder import EventKind, MemorySink, Recorder
from chronotrace.recorder.redact import REDACTED, Redactor
from chronotrace.recorder.scope import Scope

_HERE = Scope(roots=[str(Path(__file__).parent)])  # pin scope so cwd cannot change the result


def test_secret_value_never_reaches_capture(monkeypatch: Any) -> None:
    """A local named like a secret is never passed to capture(); its value is safe."""
    from chronotrace.recorder.capture import capture as real

    seen: list[object] = []

    def spy(value: object, *args: Any, **kwargs: Any) -> Any:
        seen.append(value)
        return real(value, *args, **kwargs)

    monkeypatch.setattr("chronotrace.recorder.recorder.capture", spy)

    def run() -> int:
        db_password = "hunter2"  # noqa: S105  -- the secret this test proves we never read
        safe_value = 42
        return safe_value + len(db_password)

    rec = Recorder(MemorySink(), capture_values=True, scope=_HERE)
    with rec:
        run()

    assert "hunter2" not in seen, "the secret value was read into capture() -- redaction failed"
    assert 42 in seen, "a non-secret local must still be captured normally"


def test_redacted_local_is_marked_not_missing() -> None:
    """The variable still appears, with a REDACTED marker -- hidden, not absent."""

    def run() -> str:
        api_token = "sk-live-xxxx"  # noqa: S105
        return api_token

    rec = Recorder(MemorySink(), capture_values=True, scope=_HERE)
    with rec:
        run()

    name_id = rec._names.intern("api_token")
    refs = [
        e.value_ref
        for e in rec.sink.events  # type: ignore[attr-defined]
        if e.kind is EventKind.VAR_WRITE and e.name_id == name_id and e.value_ref is not None
    ]
    assert refs, "the redacted variable must still appear in the timeline"
    assert all(rec._values.resolve(r) == REDACTED for r in refs)


def test_matching_is_case_insensitive() -> None:
    redactor = Redactor()
    assert redactor.should_redact("DB_PASSWORD")
    assert redactor.should_redact("Auth_Header")
    assert not redactor.should_redact("username")


def test_default_patterns_cover_the_common_secret_names() -> None:
    redactor = Redactor()
    for name in ("password", "db_password", "API_TOKEN", "secret_key", "apikey", "oauth"):
        assert redactor.should_redact(name), name
    for name in ("username", "count", "total", "user_id", "path"):
        assert not redactor.should_redact(name), name


def test_custom_patterns_replace_the_defaults() -> None:
    redactor = Redactor(["*key*"])
    assert redactor.should_redact("api_key")
    assert not redactor.should_redact("password"), "custom patterns are not additive to defaults"


def test_nested_secret_is_a_documented_limitation() -> None:
    """`config["password"]` binds `config`, not `password`.

    Redaction sees the local name, so the whole dict -- password included -- is
    captured. Pinned here so the limitation is honest, not accidental. See
    docs/configuration.md.
    """
    assert Redactor().should_redact("config") is False
