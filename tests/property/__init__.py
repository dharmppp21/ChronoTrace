"""Property testing: the referee, pointed at programs nobody wrote.

See `README.md` for the properties, what the generator covers, and how to reproduce a
failure from its seed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from tests.equivalence.minimise import load_program


def load_generated(workdir: Path, source: str) -> Callable[[], object]:
    """Import a generated program and return its `main`.

    The module name is a **hash of the source**, and that is load-bearing rather than
    tidy. Hypothesis re-runs a failing example to confirm it, then again while shrinking;
    if each run wrote a differently-named module the traceback would differ between runs
    and Hypothesis would declare the failure flaky and give up. That happened before the
    name became content-addressed. Determinism in the harness is a precondition for
    shrinking, not a nicety.

    Raises:
        AssertionError: the program would not import, which means the *generator* emitted
            something invalid -- a harness bug, never a finding about the code under test.
    """
    digest = hashlib.blake2b(source.encode(), digest_size=8).hexdigest()
    entry = load_program(workdir, f"gen_{digest}", source, "main")
    assert entry is not None, f"generated program failed to import:\n{source}"
    return entry
