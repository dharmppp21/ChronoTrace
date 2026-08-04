"""Assert what the built wheel contains -- and, more importantly, what it does not.

A wheel is the artifact a stranger installs, so its contents are a contract: the browser UI
must be inside it (or `serve` renders nothing), and the maintainer's `tests/`, `spikes/` and
`benchmarks/` must NOT be (a user should never download our scratch work, and it bloats the
download). These are cheap `zipfile` assertions over `dist/*.whl`; they skip when no wheel has
been built (the normal test matrix), and run in the packaging CI job that builds one first.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent

# A debugger wheel is Python source plus a ~60 KiB prebuilt SPA -- a few hundred KiB. If it ever
# crosses this, something large leaked: node_modules, source maps, or a stray data file. The real
# wheel is ~260 KiB, so 5 MiB is a wide catch-net, not a tight fit.
MAX_WHEEL_BYTES = 5 * 1024 * 1024


def _wheel() -> zipfile.ZipFile:
    matches = sorted((_ROOT / "dist").glob("*.whl"))
    if not matches:
        pytest.skip("no wheel in dist/ -- run `python -m build --wheel` first")
    return zipfile.ZipFile(matches[0])


def test_wheel_bundles_the_browser_ui() -> None:
    names = _wheel().namelist()
    assert any(n.endswith("chronotrace/_ui/index.html") for n in names), "the SPA entry is missing"
    assert any("/_ui/assets/" in n for n in names), "the built JS/CSS assets are missing"


def test_wheel_excludes_maintainer_directories() -> None:
    # _ui/ legitimately contains built asset paths; only non-_ui matches are a leak.
    leaked = [
        n
        for n in _wheel().namelist()
        if any(part in n for part in ("tests/", "spikes/", "benchmarks/")) and "/_ui/" not in n
    ]
    assert not leaked, f"maintainer directories leaked into the wheel: {leaked}"


def test_wheel_ships_no_source_maps() -> None:
    maps = [n for n in _wheel().namelist() if n.endswith(".map")]
    assert not maps, f"source maps in the wheel bloat it and expose build internals: {maps}"


def test_wheel_size_is_sane() -> None:
    wheel_path = Path(_wheel().filename or "")
    size = wheel_path.stat().st_size
    assert size < MAX_WHEEL_BYTES, f"{wheel_path.name} is {size / 1024 / 1024:.1f} MiB (a smell)"


def test_wheel_declares_the_ui_extra() -> None:
    # The [ui] extra must survive into wheel metadata, or `pip install chronotrace[ui]` is a no-op.
    z = _wheel()
    metadata = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
    text = z.read(metadata).decode()
    assert "Provides-Extra: ui" in text
    # Quote-agnostic: the metadata writer emits `extra == 'ui'` (single quotes).
    assert any("fastapi" in ln and "extra" in ln and "ui" in ln for ln in text.splitlines())
