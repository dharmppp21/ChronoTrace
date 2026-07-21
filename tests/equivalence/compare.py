"""Policy-aware comparison: what is legitimately lossy, and what is a bug.

**This is the hard part of the harness.** A lenient comparator is a test that always
passes and protects nothing, so every allowance below is named, justified, and narrow.
The rule applied throughout: an allowance is legitimate only when the system never
claimed the thing in the first place.

What is legitimately different
------------------------------
1. **Object-identity ids.** `capture` stamps a durable `id` into tagged dicts when given
   an `ObjectIdentity`. The recorder has one; the observer deliberately has none, because
   sharing the recorder's would perturb its id assignment (the observer captures first).
   Two independent identity maps cannot agree by construction -- id 3 means a different
   object in each -- so ids are stripped from both sides. The cost: aliasing is not
   checked here. It is checked in `tests/recorder/test_capture.py`.

2. **Nothing else.** Truncation, depth limits and redaction are *not* allowances: the
   observer applies the same policy and the same `Redactor`, so a truncated list must
   match a truncated list exactly, and a redacted secret must be `REDACTED` on both
   sides. A comparator that forgave truncation would forgive a value being wrong past
   element 100.

What is compared, and what is deliberately not
----------------------------------------------
**The current frame's bindings, exactly.** The recorder's contract is that it rescans
`f_locals` of the frame executing the event, so this is where it claims to be exact and
where it is held to it.

**The stack, structurally.** Every in-scope frame on the real `f_back` chain must be live
in the reconstructed state, at the right line, in the right order -- and any *extra* live
frame must be `suspended`. That pair of checks is the day-6 registry model stated as an
assertion: live frames are the stack plus suspended generators, and nothing else.

**Not** the binding values of outer frames. A caller's locals are only as fresh as the
last line that caller executed, because that is the only time the recorder rescans them
-- so if a callee mutates a list the caller holds, the reconstructed caller is stale by
design. Comparing that against `f_locals` *now* would fail the system for a claim it
never made. The staleness is real and worth knowing about; it is tracked as issue #8,
not smuggled into this file as a tolerance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import CodeType
from typing import Any

from chronotrace.reconstruct import ProgramState
from chronotrace.recorder.capture import CapturedValue

from .truth import Observation


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One disagreement between the reconstruction and reality, ready to debug.

    Carries the context needed to act without re-running anything: which instant, which
    variable, both values, and how reconstruction got there (nearest keyframe and how
    many deltas it replayed). See the README on reading one.
    """

    seq: int
    kind: str
    where: str
    name: str
    expected: Any
    actual: Any
    detail: str = ""

    def __str__(self) -> str:
        return (
            f"\n  seq {self.seq}  [{self.kind}]  {self.where}"
            f"\n    {self.name}"
            f"\n      real:          {_short(self.expected)}"
            f"\n      reconstructed: {_short(self.actual)}"
            f"\n    {self.detail}"
        )


def compare(
    state: ProgramState,
    observation: Observation,
    values: Mapping[int, CapturedValue],
    names: Mapping[int, str],
    codes: Mapping[int, CodeType],
    detail: str = "",
) -> list[Mismatch]:
    """Every way the reconstructed state disagrees with what the program really had.

    Args:
        state: the reconstruction at this instant.
        observation: the independently observed truth for the same instant.
        values: `value_ref -> captured value`, already resolved through the pool.
        names: `name_id -> variable name`.
        codes: `code_id -> code object`.
        detail: reconstruction provenance, quoted into every mismatch.

    Returns:
        Mismatches, most specific first. Empty means the two agree.

    Complexity: O(live frames + bindings at this instant).
    """
    where = _where(observation)
    frame = state.frame(state.current_frame_id)
    if frame is None:
        return [
            Mismatch(state.seq, "frame", where, "the executing frame", "live", "absent", detail)
        ]
    found = codes.get(frame.code_id)
    if found is not observation.code or frame.lineno != observation.lineno:
        return [
            Mismatch(
                state.seq,
                "frame",
                where,
                "the executing frame's position",
                f"{_name(observation.code)}:{observation.lineno}",
                f"{_name(found)}:{frame.lineno}",
                detail,
            )
        ]
    bindings = {
        names.get(name_id, f"name#{name_id}"): values[ref]
        for name_id, ref in frame.bindings.items()
    }
    return _compare_bindings(state.seq, where, observation, bindings, detail) + _compare_stack(
        state, observation, codes, where, detail
    )


def _compare_bindings(
    seq: int,
    where: str,
    observation: Observation,
    bindings: Mapping[str, CapturedValue],
    detail: str,
) -> list[Mismatch]:
    """The executing frame's locals, name by name and value by value."""
    out = []
    for name in sorted(set(observation.bindings) | set(bindings)):
        real, got = observation.bindings.get(name, _ABSENT), bindings.get(name, _ABSENT)
        if real is _ABSENT:
            out.append(Mismatch(seq, "extra", where, name, "not a local here", got, detail))
        elif got is _ABSENT:
            out.append(Mismatch(seq, "missing", where, name, real, "not reconstructed", detail))
        elif strip_ids(real) != strip_ids(got):
            out.append(Mismatch(seq, "value", where, name, real, got, detail))
    return out


def _compare_stack(
    state: ProgramState,
    observation: Observation,
    codes: Mapping[int, CodeType],
    where: str,
    detail: str,
) -> list[Mismatch]:
    """The day-6 model as an assertion: live frames are the stack plus suspended ones."""
    live = [(codes.get(f.code_id), f.lineno, f.suspended) for f in reversed(state.frames)]
    on_stack = [(code, line) for code, line, _s in live if not _s]
    real = list(observation.stack)
    if on_stack != real:
        return [
            Mismatch(
                state.seq,
                "stack",
                where,
                "the call stack",
                [f"{_name(c)}:{ln}" for c, ln in real],
                [f"{_name(c)}:{ln}" for c, ln in on_stack],
                f"{detail} (frames live but not on the stack must be suspended generators)",
            )
        ]
    return []


def strip_ids(value: CapturedValue) -> CapturedValue:
    """Drop `capture`'s object-identity stamps, recursively. See the module docstring.

    Only `id` keys of *tagged* dicts (`{"$": ...}`) are removed, so a user dict whose own
    key happens to be "id" is untouched -- user keys are captured inside `items`, never
    as top-level keys of the tagged dict.
    """
    if isinstance(value, dict):
        return {k: strip_ids(v) for k, v in value.items() if not (k == "id" and "$" in value)}
    if isinstance(value, list):
        return [strip_ids(v) for v in value]
    return value


_ABSENT = object()
""""This name was not here at all", distinct from any value it could legitimately hold.
Never rendered: both branches that test for it substitute their own wording."""


def _where(observation: Observation) -> str:
    code = observation.code
    return f"{Path(code.co_filename).name}:{observation.lineno} in {code.co_qualname}"


def _name(code: CodeType | None) -> str:
    return code.co_qualname if code is not None else "<unknown code>"


def _short(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}... ({len(text)} chars)"
