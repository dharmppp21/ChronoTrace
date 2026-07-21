"""Shrink a harness failure to the smallest program that still reproduces it.

A mismatch at event 487,332 of a million-event recording is a fact, not a lead. The
useful artefact is the eight-line program that fails the same way, and producing it by
hand is an hour of bisecting by deletion. This does it mechanically, which is the
difference between a referee you consult and one you dread.

The algorithm, and why this one
-------------------------------
Contiguous-chunk removal at halving granularity: try to delete `n` consecutive lines,
keep the deletion if the result still parses, still runs, and still fails; when a full
pass removes nothing, halve `n` and repeat down to single lines.

**Single-line greedy was written first and measured, and it is not enough.** It gets
stuck on the most ordinary input imaginable -- an unrelated helper function:

    def helper(n):        <- removing this alone leaves an orphaned indented block
        total += i        <- removing this alone leaves a function with no body

Neither line can go on its own, so both survive and the "minimal" repro still carries a
dead husk of unrelated code. Removing them as a *chunk* costs about ten extra lines here
and is what makes the output actually small. `test_minimisation_shrinks_a_real_failure`
pins that difference.

Rejected: **full `ddmin`** (Zeller), which also removes non-contiguous subsets and
complements. Strictly stronger, roughly triple the code, and the failure above is the
motivating case -- it is contiguous. Revisit if a real failure resists this.

Rejected: **AST-level shrinking**, which would guarantee syntactic validity instead of
discarding candidates that fail to parse. It needs a rewriting pass per node type, and
`compile()` is a one-line validity check costing microseconds against an oracle costing
milliseconds.

The oracle must be honest
-------------------------
`still_fails` returns True only for *the same kind of failure*. A candidate that crashes
on import, or fails for an unrelated reason, is not a smaller reproduction -- it is a
different bug, and accepting it would shrink towards nonsense. `harness_oracle` therefore
matches on the mismatch `kind` and variable name, not merely on "something went wrong".
"""

from __future__ import annotations

import importlib.util
import itertools
from collections.abc import Callable
from pathlib import Path

from chronotrace.recorder.scope import Scope

from . import check
from .compare import Mismatch

Oracle = Callable[[str], bool]
"""Does this source still reproduce the failure? Injected, so `minimise` is testable
without recording anything."""


def minimise(source: str, still_fails: Oracle) -> str:
    """The shortest source still reproducing the failure `still_fails` recognises.

    Args:
        source: the failing program.
        still_fails: the oracle. Must be true for `source` itself, or there is nothing
            to shrink towards.

    Returns:
        The reduced source. Equal to `source` if no line could be removed.

    Raises:
        ValueError: the oracle does not reproduce on the original -- shrinking would
            "succeed" by returning noise.

    Complexity: O(lines^2) oracle calls in the worst case, each a full record-and-compare.
    """
    if not still_fails(source):
        raise ValueError("the oracle does not reproduce the failure on the original source")
    lines = source.splitlines()
    while True:  # ponytail: contiguous chunks only; full ddmin if a real case resists
        before = len(lines)
        # Powers of two, not repeated halving of the line count: from 14 lines the
        # latter gives 7, 3, 1 and never tries a chunk of 2 -- exactly the size of the
        # two-line function husk this exists to remove.
        size = 1 << max(0, len(lines).bit_length() - 1)
        while size >= 1:
            lines = _pass(lines, size, still_fails)
            size //= 2
        if len(lines) == before:
            return "\n".join(lines)
        # A smaller pass can leave a chunk removable that was not before -- deleting one
        # line of a three-line husk makes the remaining two deletable together. One
        # descent down the sizes would stop there, so the whole schedule repeats.


def _pass(lines: list[str], size: int, still_fails: Oracle) -> list[str]:
    """Remove every `size`-line chunk that the failure survives, front to back."""
    i = 0
    while i < len(lines):
        candidate = lines[:i] + lines[i + size :]
        text = "\n".join(candidate)
        if len(candidate) < len(lines) and _parses(text) and still_fails(text):
            lines = candidate  # do not advance: the next chunk has shifted into place
        else:
            i += 1
    return lines


def harness_oracle(workdir: Path, like: Mismatch, *, entry: str = "main") -> Oracle:
    """An oracle that reproduces `like`: the same mismatch kind, on the same variable.

    Each candidate becomes a fresh module in `workdir` under a unique name, imported and
    recorded. A candidate that will not import, or has no entry point, is not a
    reproduction -- see the module docstring on why that matters.
    """
    counter = itertools.count()
    scope = Scope(roots=[str(workdir)])

    def still_fails(source: str) -> bool:
        entry_point = load_program(workdir, f"_shrink{next(counter)}", source, entry)
        if entry_point is None:
            return False
        try:
            found = check(entry_point, scope)
        except Exception:
            return False
        return any(m.kind == like.kind and m.name == like.name for m in found)

    return still_fails


def load_program(workdir: Path, name: str, source: str, entry: str) -> Callable[[], object] | None:
    """Write, import and return `source`'s entry point, or None if it will not load.

    Shared with the day-23 property campaign, which turns generated source into something
    recordable exactly the same way. The caller supplies `name` because the two have
    different needs: minimisation counts upwards, while the campaign hashes the source so
    a Hypothesis replay of the same program loads the same module -- see
    `tests/property/test_pipeline.py` on why that matters for shrinking.
    """
    path = workdir / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    found = getattr(module, entry, None)
    return found if callable(found) else None


def _parses(source: str) -> bool:
    try:
        compile(source, "<candidate>", "exec")
    except (SyntaxError, ValueError):
        return False
    return True
