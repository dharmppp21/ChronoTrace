"""Pins self-exclusion: ChronoTrace must never record ChronoTrace."""

from __future__ import annotations

from pathlib import Path

from chronotrace.recorder.scope import Scope


def test_excludes_chronotrace_itself() -> None:
    """The default scope refuses our own package. Infinite regress otherwise."""
    import chronotrace.recorder.recorder as rec_mod

    assert Scope().allows(rec_mod.__file__) is False


def test_allows_ordinary_user_code() -> None:
    assert Scope().allows(str(Path.cwd() / "user_app.py")) is True


def test_caches_the_decision() -> None:
    """One dict probe per event, not one `startswith` per event.

    Day 2 measured a naive per-event string comparison at 19-27% overhead on
    in-scope-heavy code. This asserts the cache exists rather than trusting it.
    """
    scope = Scope(excluded_root="/pkg")
    scope.allows("/pkg/a.py")
    scope.allows("/other/b.py")
    assert scope._cache == {"/pkg/a.py": False, "/other/b.py": True}


def test_synthetic_filenames_are_allowed() -> None:
    """`exec`-ed code has no real path.

    `<string>` is not ours, so it is recorded. Day 9 revisits whether users want
    exec'd code in scope; today the only rule is "not chronotrace".
    """
    assert Scope().allows("<string>") is True
    assert Scope().allows("<frozen importlib._bootstrap>") is True


def test_a_path_merely_containing_the_root_is_still_excluded_only_by_prefix() -> None:
    """`startswith`, not `in`.

    A user directory named `/home/me/chronotrace_clone/app.py` must still be
    recorded when our root is `/site-packages/chronotrace`. Substring matching
    would silently blind the recorder to that user's code.
    """
    scope = Scope(excluded_root="/site-packages/chronotrace")
    assert scope.allows("/home/me/chronotrace_clone/app.py") is True
    assert scope.allows("/site-packages/chronotrace/recorder/sink.py") is False
