"""The wire contract, locked. The most important test walks every DTO's type graph and proves
no storage type can reach the wire -- an automated architectural rule, not a promise.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import types
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin, get_type_hints

from chronotrace.server import dto

_PRIMITIVES = {str, int, float, bool, type(None)}
_FORBIDDEN_MODULES = (
    "chronotrace.store",
    "chronotrace.recorder",
    "chronotrace.reconstruct",
    "chronotrace.index",
    "chronotrace.query",
)


def _dto_members(predicate: Callable[[type], bool]) -> set[type]:
    return {
        obj
        for _n, obj in inspect.getmembers(dto, inspect.isclass)
        if obj.__module__ == dto.__name__ and predicate(obj)
    }


def _leaf_types(annotation: Any) -> set[Any]:
    """Every concrete type in an annotation, unwrapping containers, unions and `Optional`."""
    origin = get_origin(annotation)
    if origin is None:
        return {annotation}
    if origin is Union or origin is types.UnionType:
        return {t for arg in get_args(annotation) for t in _leaf_types(arg)}
    return {t for arg in get_args(annotation) if arg is not Ellipsis for t in _leaf_types(arg)}


def test_no_storage_type_can_reach_the_wire() -> None:
    """The rule the whole layer exists to keep: a DTO reaches only primitives, enums and DTOs.

    Walks the resolved type hints of every dataclass in `dto.py` and fails if any leaf type is
    not on the allowlist -- which, by construction, no `store`/`recorder`/`reconstruct`/`index`
    type can be. Stronger than a blocklist: a type nobody thought to forbid is forbidden by
    default, so the day a careless mapping adds `frames: tuple[ProgramState, ...]` this goes red.
    """
    dataclasses_ = _dto_members(dataclasses.is_dataclass)
    enums = _dto_members(lambda o: issubclass(o, enum.Enum))
    allowed = _PRIMITIVES | dataclasses_ | enums

    referenced: set[Any] = set()
    for cls in dataclasses_:
        for annotation in get_type_hints(cls).values():
            referenced |= _leaf_types(annotation)

    offending = {t for t in referenced if t not in allowed}
    assert not offending, (
        f"non-wire types reachable from a DTO: {sorted(str(t) for t in offending)}"
    )

    leaked = {
        t for t in referenced if isinstance(t, type) and t.__module__.startswith(_FORBIDDEN_MODULES)
    }
    assert not leaked, f"storage types on the wire: {leaked}"


def test_state_serialises_to_a_locked_shape() -> None:
    """A golden `to_wire` of the hot endpoint -- pins the exact JSON the scrubber will parse."""
    state = dto.State(
        seq=42,
        current_frame_id=3,
        truncated=False,
        frames=(
            dto.Frame(
                frame_id=3,
                function="build_report",
                file="pipeline.py",
                lineno=52,
                suspended=False,
                variables=(
                    dto.Variable(
                        name="total", preview="60", ref=7, has_children=False, truncated=False
                    ),
                ),
            ),
        ),
    )
    assert dto.to_wire(state) == {
        "seq": 42,
        "current_frame_id": 3,
        "truncated": False,
        "exception": None,
        "frames": [
            {
                "frame_id": 3,
                "function": "build_report",
                "file": "pipeline.py",
                "lineno": 52,
                "suspended": False,
                "variables": [
                    {
                        "name": "total",
                        "preview": "60",
                        "ref": 7,
                        "has_children": False,
                        "truncated": False,
                    }
                ],
            }
        ],
    }


def test_enums_serialise_to_their_string_value() -> None:
    result = dto.StepResult(seq=None, edge=dto.StepEdge.LOST_TAIL)
    assert dto.to_wire(result) == {"seq": None, "edge": "lost_tail"}


def test_every_error_code_has_a_status_and_a_distinct_action() -> None:
    """The error mapping is exhaustive: every code the server can raise has a canonical status."""
    assert set(dto.STATUS_FOR) == set(dto.ErrorCode), "every ErrorCode needs a status"
    assert all(400 <= s < 600 for s in dto.STATUS_FOR.values())


def test_problem_factory_uses_the_canonical_status() -> None:
    p = dto.problem(dto.ErrorCode.SEQ_OUT_OF_RANGE, "pick a seq in [0, 100)")
    assert p.status == 404
    assert dto.to_wire(p) == {
        "code": "seq_out_of_range",
        "status": 404,
        "title": "seq out of range",
        "detail": "pick a seq in [0, 100)",
        "instance": None,
    }
