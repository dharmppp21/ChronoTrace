"""Scope resolution and the DISABLE win: the callback stops being called.

The load-bearing test is `test_disable_stops_the_callback_for_out_of_scope_code`.
Everything else pins a corner of the decision order or the path normalisation.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any

import pytest

from chronotrace.recorder import MemorySink, Recorder
from chronotrace.recorder.scope import Scope


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("chrono_tmp_helper", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _CountingRecorder(Recorder):
    """Counts how often the LINE callback is actually invoked, per file."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.line_calls: dict[str, int] = {}

    def _on_line(self, code: CodeType, line_number: int) -> Any:
        self.line_calls[code.co_filename] = self.line_calls.get(code.co_filename, 0) + 1
        return super()._on_line(code, line_number)


def test_disable_stops_the_callback_for_out_of_scope_code(tmp_path: Path) -> None:
    """The whole point of DISABLE: CPython stops calling us. Asserted directly.

    A 3-line function called 50 times. In scope, the LINE callback fires per line
    per call. Out of scope, each line's location returns DISABLE on first sight
    and is never called again -- so the count is a small constant, not 50x.
    """
    helper = tmp_path / "helper.py"
    helper.write_text(
        "def work(a):\n    b = a + 1\n    c = b + 1\n    return c\n", encoding="utf-8"
    )
    mod = _load(helper)

    in_scope = _CountingRecorder(MemorySink(), scope=Scope(roots=[str(tmp_path)]))
    with in_scope:
        for i in range(50):
            mod.work(i)

    out_scope = _CountingRecorder(MemorySink(), scope=Scope(roots=[str(tmp_path / "elsewhere")]))
    with out_scope:
        for i in range(50):
            mod.work(i)

    key = helper.__str__()
    assert in_scope.line_calls.get(key, 0) >= 50, "in-scope code must be recorded every call"
    assert out_scope.line_calls.get(key, 0) <= 4, "DISABLE must de-instrument after first sight"
    assert not out_scope.sink.events, "out-of-scope code produced no events"  # type: ignore[attr-defined]


def test_excludes_chronotrace_itself() -> None:
    """Recording our own code is infinite regress. Non-negotiable, any config."""
    import chronotrace.recorder.recorder as rec_mod

    assert Scope().allows(rec_mod.__file__) is False
    assert Scope(roots=[str(Path(rec_mod.__file__).parent)]).allows(rec_mod.__file__) is False


def test_stdlib_and_site_packages_excluded_by_default() -> None:
    """Nobody wants json/decoder.py in their timeline; a dep is not "my code"."""
    assert Scope().allows(json.__file__) is False  # stdlib
    assert Scope().allows(pytest.__file__) is False  # site-packages


def test_user_code_under_a_root_is_allowed(tmp_path: Path) -> None:
    scope = Scope(roots=[str(tmp_path)])
    assert scope.allows(str(tmp_path / "app.py")) is True
    assert scope.allows(str(tmp_path.parent / "other" / "app.py")) is False


def test_include_glob_overrides_the_library_exclusion() -> None:
    """Debugging into a dependency: opt it back in by glob."""
    assert Scope(include=["*/json/*"]).allows(json.__file__) is True


def test_exclude_glob_overrides_a_root(tmp_path: Path) -> None:
    scope = Scope(roots=[str(tmp_path)], exclude=["*/migrations/*"])
    assert scope.allows(str(tmp_path / "migrations" / "0001.py")) is False
    assert scope.allows(str(tmp_path / "models.py")) is True


def test_synthetic_filenames_excluded_by_default_but_includable() -> None:
    """exec/eval/frozen code has no on-disk source; exclude unless asked for."""
    assert Scope(roots=["/anywhere"]).allows("<string>") is False
    assert Scope().allows("<frozen importlib._bootstrap>") is False
    assert Scope(include=["<string>"]).allows("<string>") is True


def test_a_sibling_sharing_a_name_prefix_is_not_swept_in(tmp_path: Path) -> None:
    """`startswith` with a boundary, not a raw substring.

    `/proj` must not put `/proj2` in scope. Without the `/` boundary a user's
    `myproject_notes/` next to `myproject/` would be silently recorded or dropped.
    """
    root = tmp_path / "proj"
    root.mkdir()
    scope = Scope(roots=[str(root)])
    assert scope.allows(str(root / "a.py")) is True
    assert scope.allows(str(tmp_path / "proj2" / "a.py")) is False


def test_the_decision_is_cached(tmp_path: Path) -> None:
    """One dict probe per event, not a path resolution. Assert the cache exists."""
    scope = Scope(roots=[str(tmp_path)])
    f = str(tmp_path / "a.py")
    scope.allows(f)
    assert f in scope._cache


def test_path_case_handling_matches_the_platform(tmp_path: Path) -> None:
    """Windows folds case; POSIX does not. A scope bug that only shows in CI."""
    root = tmp_path / "Proj"
    root.mkdir()
    scope = Scope(roots=[str(root)])
    same_file_other_case = str(root / "App.py").upper()
    if os.name == "nt":
        assert scope.allows(same_file_other_case) is True
    else:
        assert scope.allows(same_file_other_case) is False
