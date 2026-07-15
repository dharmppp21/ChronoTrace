"""Proves capture's three invariants: bounded, never runs user code, never retains."""

from __future__ import annotations

import gc
import json
import weakref
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chronotrace.recorder.capture import DEFAULT_POLICY, CapturePolicy, capture
from chronotrace.recorder.identity import ObjectIdentity
from tests.fixtures import hostile


@pytest.fixture(scope="module")
def zoo() -> dict[str, Any]:
    """Built once: the 10M list and 10k-deep dict cost ~0.5s and real memory."""
    return hostile.build_zoo()


CASES = [
    "self_referential_list",
    "mutual_pair",
    "huge_list",
    "deep_dict",
    "wide_and_deep",
    "repr_explodes",
    "property_side_effects",
    "fabricates_attributes",
    "lies_about_its_class",
    "generator",
    "open_file",
    "socket",
    "lock",
    "fake_array",
    "slotted",
    "weakref",
    "long_string",
    "nested_mixed",
]


# ---------------------------------------------------------------------------
# Invariant 1: bounded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_every_hostile_case_is_bounded(zoo: dict[str, Any], case: str) -> None:
    """Terminates, stays in budget, never raises.

    The byte ceiling is the real assertion: a capture that "succeeds" by emitting
    416 MB has not succeeded, and that is not hypothetical -- it is what the day 3
    policy did to `wide_and_deep` before `max_nodes` existed.
    """
    out = capture(zoo[case])
    assert len(json.dumps(out, default=str)) < 32_768, f"{case} blew the byte budget"


def test_wide_and_deep_is_bounded_by_the_node_budget(zoo: dict[str, Any]) -> None:
    """The hole day 3's zoo never found.

    max_depth=6 x max_items=100 permits 1e12 nodes. Depth and width limits bound
    each dimension; only a total budget bounds their product. Measured before the
    fix: 26 seconds, 416 MB, for one variable on one line.
    """
    out = capture(zoo["wide_and_deep"])
    assert "budget" in json.dumps(out), "the node budget should have been hit"
    assert len(json.dumps(out, default=str)) < 32_768


def test_depth_limit_marks_rather_than_recurses(zoo: dict[str, Any]) -> None:
    out = capture(zoo["deep_dict"])
    assert "depth" in json.dumps(out)


def test_deep_data_does_not_raise_recursion_error(zoo: dict[str, Any]) -> None:
    """max_depth -- not the data -- bounds the stack. Measured: 7 frames on 10k deep."""
    capture(zoo["deep_dict"])  # would raise if a path escaped the depth limit


def test_truncation_is_visible_not_silent(zoo: dict[str, Any]) -> None:
    """A lossy tool is fine; a lying one is not.

    Showing 100 of 10,000,000 items without saying so teaches the user the list has
    100 items, and they debug the wrong thing.
    """
    out = capture(zoo["huge_list"])
    assert out["truncated"] is True
    assert out["len"] == 10_000_000
    assert len(out["items"]) == DEFAULT_POLICY.max_items


def test_long_string_reports_its_real_length(zoo: dict[str, Any]) -> None:
    out = capture(zoo["long_string"])
    assert out["truncated"] is True
    assert out["len"] == 5_000_000
    assert len(out["head"]) == DEFAULT_POLICY.max_str_len


def test_policy_is_honoured(zoo: dict[str, Any]) -> None:
    tight = CapturePolicy(max_depth=2, max_items=3, max_str_len=8, max_nodes=16)
    out = capture(zoo["nested_mixed"], tight)
    assert len(json.dumps(out, default=str)) < 512


# ---------------------------------------------------------------------------
# Invariant 2: never invokes user code
# ---------------------------------------------------------------------------


def test_capture_never_invokes_user_code(zoo: dict[str, Any]) -> None:
    """The invariant separating observing from participating.

    If capture runs a `__repr__` or a property, the debugger has changed the
    program it is watching, and can hide the very bug being hunted. stdlib's
    reprlib fails exactly this, which is why it was rejected.
    """
    hostile.reset_sentinels()
    for name, value in zoo.items():
        if not name.startswith("_"):
            capture(value)
    assert hostile.EXPLODED is False, "capture called a user __repr__"
    assert hostile.TOUCHED is False, "capture read a user property or __getattr__"


def test_raising_repr_is_captured_without_being_called(zoo: dict[str, Any]) -> None:
    hostile.reset_sentinels()
    out = capture(zoo["repr_explodes"])
    assert out["type"] == "ReprExplodes"
    assert hostile.EXPLODED is False


def test_slots_are_read_without_firing_getattr(zoo: dict[str, Any]) -> None:
    """Unset slots must not raise into `__getattr__` -- user code via the error path."""
    hostile.reset_sentinels()
    out = capture(zoo["slotted"])
    assert out["attrs"]["x"] == 1
    assert out["attrs"]["y"] == "two"
    assert "never_set" not in out["attrs"], "an unset slot must be skipped, not invented"


def test_opaque_resources_are_summarised_never_touched(zoo: dict[str, Any]) -> None:
    """A captured socket is meaningless; a captured file handle is a leak."""
    for case in ("socket", "lock", "generator", "open_file"):
        out = capture(zoo[case])
        assert out["$"] == "obj"
        assert "type" in out


def test_huge_buffer_is_described_not_copied(zoo: dict[str, Any]) -> None:
    out = capture(zoo["fake_array"])
    assert out["buffer"]["nbytes"] == 4_000_000_000
    assert len(json.dumps(out)) < 512, "the 4GB buffer must not be in the output"


# ---------------------------------------------------------------------------
# Invariant 3: never retains
# ---------------------------------------------------------------------------


def test_capture_does_not_retain_the_object() -> None:
    """Retaining changes when the program's finalisers run.

    The debugger would alter the timing of the thing it observes -- and could mask
    the refcount bug being hunted.
    """

    class Tracked:
        def __init__(self) -> None:
            self.payload = [1, 2, 3]

    obj = Tracked()
    ref = weakref.ref(obj)
    captured = capture(obj)
    assert captured["attrs"]["payload"]["items"] == [1, 2, 3]

    del obj
    gc.collect()
    assert ref() is None, "capture retained the object"


def test_identity_map_does_not_retain_either() -> None:
    """The weak map must not become the leak the weakness exists to prevent."""

    class Tracked:
        pass

    identity = ObjectIdentity()
    obj = Tracked()
    ref = weakref.ref(obj)
    capture(obj, identity=identity)

    del obj
    gc.collect()
    assert ref() is None, "the identity map retained the object"


# ---------------------------------------------------------------------------
# Cycles and identity
# ---------------------------------------------------------------------------


def test_self_referential_list_terminates_with_a_back_reference(zoo: dict[str, Any]) -> None:
    out = capture(zoo["self_referential_list"])
    assert "cycle" in json.dumps(out, default=str)


def test_mutual_references_terminate(zoo: dict[str, Any]) -> None:
    out = capture(zoo["mutual_pair"])
    assert "cycle" in json.dumps(out, default=str)


def test_same_object_twice_gets_one_identity() -> None:
    """The aliasing badge: two names, one object.

    Works for weakref-able objects, which custom classes are.
    """

    class Shared:
        def __init__(self) -> None:
            self.n = 1

    shared = Shared()
    identity = ObjectIdentity()
    a = capture(shared, identity=identity)
    b = capture(shared, identity=identity)
    assert a["id"] == b["id"]


def test_atoms_have_no_identity() -> None:
    """int/str are not weakref-able, and identity for them means nothing.

    Two equal ints are indistinguishable in every way a debugger can show. Falling
    back to `id()` would reintroduce the reuse bug for exactly the values that gain
    nothing from having identity.
    """
    identity = ObjectIdentity()
    assert identity.of(42) is None
    assert identity.of("hello") is None
    assert identity.of((1, 2)) is None


def test_builtin_containers_get_no_durable_identity() -> None:
    """The gap, pinned so it cannot be forgotten.

    dict and list cannot hold a weak reference, so they get no id -- and they are
    exactly where aliasing bugs live (examples/buggy_pipeline.py mutates a dict
    through an alias). No identity beats a wrong one, but day 37 must resolve this
    before the badge ships. If a future Python makes dicts weakref-able, this test
    fails and the gap has closed.
    """
    identity = ObjectIdentity()
    assert identity.of({"a": 1}) is None
    assert identity.of([1, 2]) is None
    assert identity.of(set()) is not None, "sets ARE weakref-able"


# ---------------------------------------------------------------------------
# Property-based
# ---------------------------------------------------------------------------

_json_like = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: (
        st.lists(children, max_size=8) | st.dictionaries(st.text(max_size=8), children, max_size=8)
    ),
    max_leaves=60,
)


@given(_json_like)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_random_structures_stay_within_budget(payload: Any) -> None:
    """Whatever hypothesis invents, the output is bounded and serialisable."""
    out = capture(payload)
    assert len(json.dumps(out, default=str)) < 32_768
