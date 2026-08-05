"""Hatchling build hook: compile the Vite frontend into the wheel, so installing needs no Node.

**Problem it solves.** The browser UI is a TypeScript/Vite app that must be *built* by Node,
but a user who runs `pip install chronotrace[ui]` must never need a JS toolchain — most of the
audience does not have Node, and a debugger that demands one at install is unusable for them. So
the frontend is built *here*, at **wheel-build time** (on the maintainer's / CI machine), and the
resulting static assets ride inside the wheel. Install time and runtime touch only prebuilt files.

**Interface.** Hatchling calls `initialize()` once, before it collects the files for a target.
It produces `src/chronotrace/_ui/`; `[tool.hatch.build.targets.wheel].artifacts` (in pyproject)
is what actually bundles that directory — this hook only makes sure it exists and is current.

**What it must never know.** How ChronoTrace records or serves anything. It is a pure
source→assets step: `frontend/` in, `_ui/` out. It imports nothing from the package.

**What can go wrong, and the degrade path (never a hard failure for a legitimate case):**
  * *No `frontend/` source* (building from an sdist, which ships source but not the JS app) →
    bundle whatever `_ui/` already exists; ship an API-only wheel otherwise. Not an error: an
    sdist install without Node is a supported, documented, API-only mode.
  * *No `npm`* on the build machine → same: use a prebuilt `_ui/` if present, else API-only.
  * *`npm` present but the build fails* → that IS a hard error. We intended to build the UI and
    could not; shipping a wheel with a broken or missing UI silently is the failure this prevents.

Reproducibility: `npm ci` (not `npm install`) installs the exact `frontend/package-lock.json`
tree, so identical inputs give identical assets. Set `CHRONOTRACE_SKIP_UI_BUILD=1` to reuse an
existing `_ui/` without invoking Node (used when a build system has already produced the assets).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBuildHook(BuildHookInterface):  # type: ignore[type-arg]  # hatchling base is untyped
    """Build `frontend/` into `src/chronotrace/_ui/` before the wheel is assembled."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Run the Vite build for the wheel target; no-op (or reuse) otherwise."""
        if self.target_name != "wheel":
            return  # the sdist ships source, not built assets

        root = Path(self.root)
        frontend = root / "frontend"
        out = root / "src" / "chronotrace" / "_ui"

        if os.environ.get("CHRONOTRACE_SKIP_UI_BUILD"):
            self._note_prebuilt(out, reason="CHRONOTRACE_SKIP_UI_BUILD is set")
            return
        # Require the lockfile specifically: we use `npm ci`, which refuses to run without one.
        # An sdist carries only a partial frontend/ (no lockfile), so this is the guard that lets
        # `pip install` from an sdist DEGRADE to an API-only wheel instead of hard-failing on
        # `npm ci`. A full source checkout has the lockfile and builds the real UI.
        lockfile = frontend / "package-lock.json"
        if not lockfile.is_file():
            self._note_prebuilt(out, reason="no frontend/package-lock.json (partial tree)")
            return

        npm = shutil.which("npm")
        if npm is None:
            self._note_prebuilt(out, reason="npm not found")
            return

        # npm present -> build for real; a failure here is a real failure (see module docstring).
        self.app.display_info("chronotrace: building the frontend (npm ci && npm run build)...")
        subprocess.run([npm, "ci"], cwd=frontend, check=True)  # noqa: S603 -- npm from which(), fixed args
        subprocess.run([npm, "run", "build"], cwd=frontend, check=True)  # noqa: S603
        self.app.display_info(f"chronotrace: frontend built -> {out.relative_to(root)}")

    def _note_prebuilt(self, out: Path, *, reason: str) -> None:
        """Report whether a prebuilt UI will ship when we cannot build one ourselves."""
        if out.is_dir() and any(out.iterdir()):
            self.app.display_info(f"chronotrace: {reason}; bundling the existing prebuilt _ui/.")
        else:
            self.app.display_warning(
                f"chronotrace: {reason} and no prebuilt _ui/; wheel will be API-only (no UI)."
            )
