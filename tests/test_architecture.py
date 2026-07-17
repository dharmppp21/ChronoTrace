"""Enforces the one-way layer dependency, so the rule is code, not a promise.

The architecture is `server -> query -> reconstruct -> index -> store -> recorder`:
each layer may import only layers below it. A rule that lives in a README is a
preference; this parses every source file's imports and fails the build if any
layer reaches upward. It is deliberately forward-proof -- `store`, `index` and the
rest do not exist yet, but the moment one does and imports `query`, this test goes
red without anyone remembering to update it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src" / "chronotrace"

# Low -> high. A file in one layer may import its own layer and any lower one,
# never a higher one. Top-level modules (config.py, cli.py) are the application
# above every layer, so they may import anything.
_LAYERS = ("recorder", "store", "index", "reconstruct", "query", "server")
_RANK = {name: i for i, name in enumerate(_LAYERS)}
_APP_RANK = len(_LAYERS)


def _rank_of_file(path: Path) -> int:
    """Which layer a source file belongs to, by its path under src/chronotrace/."""
    parts = path.relative_to(_SRC).parts
    if parts and parts[0] in _RANK:
        return _RANK[parts[0]]
    return _APP_RANK  # a top-level module: chronotrace/config.py, cli.py, ...


def _rank_of_import(module: str) -> int:
    """Which layer an imported `chronotrace.<x>...` module belongs to."""
    parts = module.split(".")
    if len(parts) >= 2 and parts[1] in _RANK:
        return _RANK[parts[1]]
    return _APP_RANK  # chronotrace.config / chronotrace itself: application level


def _chronotrace_imports(tree: ast.Module) -> list[str]:
    """Every `chronotrace.*` module a file imports (absolute imports only).

    Relative imports (`from .scope import`) can only reach within the same package
    -- the same layer -- so they can never violate the rule and are skipped.
    """
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [n.name for n in node.names if n.name.startswith("chronotrace.")]
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.startswith("chronotrace.")
        ):
            modules.append(node.module)
    return modules


def test_no_layer_imports_a_higher_layer() -> None:
    """The dependency arrow points one way. Verified by parsing, not trusted.

    The load-bearing case is the recorder (layer 0): it is imported into the user's
    own process, so a stray import of `store` would drag the file format -- and its
    dependencies -- into every recorded program. This checks every layer at once,
    so that case is covered without a second, subset test.
    """
    violations: list[str] = []
    for path in _SRC.rglob("*.py"):
        file_rank = _rank_of_file(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in _chronotrace_imports(tree):
            if _rank_of_import(module) > file_rank:
                rel = path.relative_to(_SRC)
                violations.append(f"{rel} (layer {file_rank}) imports {module}")
    assert not violations, "upward imports break the architecture:\n" + "\n".join(violations)
