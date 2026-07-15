"""Smoke tests for the packaging baseline.

These do not test behaviour -- there is none yet. They test the two Day 1
decisions that everything else rests on, because a decision documented but
unenforced is just a comment.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version

import chronotrace


def test_installed_metadata_matches_package_version() -> None:
    """The version has exactly one source of truth.

    ``chronotrace.__version__`` is a literal in ``_version.py``; the distribution
    metadata is produced from it by hatchling's version hook. If the hook is ever
    misconfigured, these two drift apart and ``chronotrace --version`` starts
    lying in bug reports. Fail here instead.
    """
    assert version("chronotrace") == chronotrace.__version__


def test_top_level_import_does_not_pull_in_subsystems() -> None:
    """``import chronotrace`` must stay light.

    The package is imported into the process being debugged, so anything it
    imports at module scope becomes the user's problem: extra entries in their
    ``sys.modules``, extra startup cost, extra surface for version conflicts.
    The top-level package therefore exposes the version and nothing else.

    Runs in a subprocess because this test suite has already imported half the
    package; only a fresh interpreter can answer the question honestly.
    """
    probe = (
        "import chronotrace, sys, json;"
        "print(json.dumps(sorted("
        "  m for m in sys.modules"
        "  if m.startswith('chronotrace.') and m != 'chronotrace._version'"
        ")))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", (
        f"importing chronotrace eagerly pulled in: {result.stdout.strip()}"
    )
