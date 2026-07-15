"""Turns any Python object into bounded, honest, serialisable data.

This is the file most likely to be the source of a nasty bug in week 8: it meets
every object a user's program contains, in the hot path, and must never flinch.

Three invariants
----------------
1. **Bounded.** Depth, item count, string length *and total node count* are all
   capped. Every one of those four is load-bearing -- see the budget note below.
2. **Never invokes user code.** Structural, not defensive: attributes are read from
   `obj.__dict__`, and properties and `__getattr__` live on the *type*, so an
   instance dict cannot reach them. `getattr` would fire them; we never call it.
   This is a **correctness** rule, not a performance one. A property with a side
   effect would make the debugger *cause* the bug it is watching for, and a
   `__repr__` that raises would take down the callback -- which day 5 measured as
   fatal to the target program, not merely to us.
3. **Never retains.** Returns plain data and holds no reference to the input. The
   recorder must not extend a recorded object's lifetime; doing so changes when
   finalisers run and can mask the refcount bug being debugged.

Why not stdlib
--------------
`reprlib.Repr` has exactly this policy shape and was rejected on day 3 with
evidence: it **ran** the user's `__repr__` (sentinel fired), and it returns a
string, which cannot be expanded, diffed, or carry an identity badge.
`tests/recorder/test_spike_capture.py::test_reprlib_does_invoke_user_code` pins
that rejection -- if a future Python makes reprlib safe, that test fails and this
file should be deleted.

Why recursive, against the day-7 brief
--------------------------------------
The brief called for an explicit work stack "because user data can be 10 000
deep". Measured: `max_depth` -- not the data -- bounds the stack. Capturing a
10,000-deep dict adds **7 frames**. The real risk is different: capture runs inside
a callback already deep in the user's stack, and it raises RecursionError when the
user's own stack is within ~5 frames of the limit. An iterative walk would move
that cliff from ~995 to ~998. It does not solve deep-stack capture; it shifts it by
three frames, for considerably harder code. Recursion, bounded by policy, is the
honest choice. The residual gap is tracked, not hidden.

Representation
--------------
Plain nested dicts/lists/atoms, never a class hierarchy: the output is directly
msgpack- and json-serialisable with no encoder, and a class would be an
abstraction over data that is already the right shape. Atoms pass through
unwrapped; every container is wrapped in a tagged dict, so a user dict containing
a `"$"` key can never be confused with our tag.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from chronotrace.recorder.identity import ObjectIdentity

type CapturedValue = Any
"""What `capture` returns: nested dicts, lists and atoms.

A name, not a class. See the module docstring on why this is data rather than a
type hierarchy.
"""


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    """What we are willing to spend on one value.

    Every default comes from a measurement, not taste.

    Attributes:
        max_depth: stop recursing here. 6 -- a 10,000-deep dict captures to 426
            bytes in 8 us (day 3).
        max_items: elements per container. 100 -- a 10M-element list captures to
            450 bytes in 17 us (day 3).
        max_str_len: characters or bytes of a string. 512 -- a 5M-char string
            captures to 571 bytes in 0.6 us (day 3).
        max_nodes: **total** nodes in one captured value. 512.

            This one exists because day 3's policy had a hole its zoo never
            found. Depth and item limits bound each *dimension*; their product is
            unbounded. `max_depth=6` with `max_items=100` permits 100**6 = 1e12
            nodes, and a perfectly ordinary 20x20x20x20x20 nested list -- not a
            contrived shape -- measured at **26 seconds and 416 MB for one
            variable on one line**. Depth and width limits are not enough; only a
            total budget bounds the product.
    """

    max_depth: int = 6
    max_items: int = 100
    max_str_len: int = 512
    max_nodes: int = 512


DEFAULT_POLICY = CapturePolicy()


@dataclass(slots=True)
class _Walk:
    """State shared across one capture: policy, cycle set, remaining budget.

    One object threaded through the recursion rather than three parameters. The
    budget in particular *must* be shared -- a per-branch budget would let a wide
    tree spend it once per branch, which is the explosion it exists to prevent.
    """

    policy: CapturePolicy
    identity: ObjectIdentity | None
    seen: set[int]
    budget: int


def capture(
    obj: object,
    policy: CapturePolicy = DEFAULT_POLICY,
    identity: ObjectIdentity | None = None,
) -> CapturedValue:
    """Turn any object into bounded, serialisable, honest data.

    Args:
        obj: anything at all, including hostile input.
        policy: what we are willing to spend.
        identity: assigns durable object ids for the UI's aliasing badges. Omit
            and captured objects carry no id at all. Deliberately not a
            throwaway map per call: that would hand every capture an id=1 that
            looks durable and is not, and this file's own rule is that no
            identity beats a wrong one.

    Returns:
        Nested dicts/lists/atoms. Never raises.

    Complexity: O(min(nodes, max_nodes)) time and output size -- bounded by policy,
    never by the size or shape of the input graph.
    """
    walk = _Walk(policy=policy, identity=identity, seen=set(), budget=policy.max_nodes)
    return _capture(obj, 0, walk)


def _capture(obj: object, depth: int, walk: _Walk) -> CapturedValue:
    if walk.budget <= 0:
        return {"$": "budget"}
    walk.budget -= 1

    handler = _handler_for(type(obj))
    return handler(obj, depth, walk)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _atom(obj: object, depth: int, walk: _Walk) -> CapturedValue:
    return obj


def _string(obj: object, depth: int, walk: _Walk) -> CapturedValue:
    """Truncation is data, never silence.

    A user shown 512 of 5,000,000 characters with no marker believes the string is
    512 characters long and debugs the wrong thing. That is the difference between
    a lossy tool and a lying one, and a lying debugger is uninstalled.
    """
    if isinstance(obj, str):
        text, tag, true_len = obj, "str", len(obj)
    else:
        raw = bytes(obj)  # type: ignore[call-overload]
        text, tag, true_len = raw.hex(), "bytes", len(raw)
    limit = walk.policy.max_str_len
    if len(text) <= limit:
        return text if tag == "str" else {"$": "bytes", "v": text}
    return {"$": tag, "head": text[:limit], "len": true_len, "truncated": True}


def _sequence(obj: object, depth: int, walk: _Walk) -> CapturedValue:
    cycle = _cycle_or_depth(obj, depth, walk)
    if cycle is not None:
        return cycle
    limit = walk.policy.max_items
    # islice, never list(obj)[:limit]: the latter materialises the whole container
    # before slicing it -- day 3 measured 70ms for a 10M-element list against 17us.
    items = [_capture(v, depth + 1, walk) for v in itertools.islice(obj, limit)]  # type: ignore[call-overload]
    total = _safe_len(obj)
    return _tagged(obj, walk, type(obj).__name__, items=items, len=total, truncated=total > limit)


def _mapping(obj: object, depth: int, walk: _Walk) -> CapturedValue:
    cycle = _cycle_or_depth(obj, depth, walk)
    if cycle is not None:
        return cycle
    limit = walk.policy.max_items
    pairs = [
        [_capture(k, depth + 1, walk), _capture(v, depth + 1, walk)]
        for k, v in itertools.islice(obj.items(), limit)  # type: ignore[attr-defined]
    ]
    total = _safe_len(obj)
    return _tagged(obj, walk, "dict", items=pairs, len=total, truncated=total > limit)


def _object(obj: object, depth: int, walk: _Walk) -> CapturedValue:
    """An arbitrary object: type metadata plus instance state, never the resource.

    Sockets, locks, files and generators have no inspectable state and get a type
    summary. A captured socket is meaningless; a captured file handle is a leak.
    """
    cycle = _cycle_or_depth(obj, depth, walk)
    if cycle is not None:
        return cycle

    attrs = _read_state(obj)
    if attrs is None:
        return _tagged(obj, walk, "obj", opaque=True)

    # An object claiming a buffer far too large to hold: describe it, never copy.
    if {"shape", "nbytes"} <= attrs.keys():
        return _tagged(
            obj, walk, "obj", buffer={k: attrs.get(k) for k in ("shape", "dtype", "nbytes")}
        )

    limit = walk.policy.max_items
    captured = {
        str(k): _capture(v, depth + 1, walk) for k, v in itertools.islice(attrs.items(), limit)
    }
    return _tagged(obj, walk, "obj", attrs=captured, truncated=len(attrs) > limit)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _tagged(obj: object, walk: _Walk, tag: str, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"$": tag}
    if tag == "obj":
        out["type"] = type(obj).__name__
        out["module"] = getattr(type(obj), "__module__", "?")
    obj_id = walk.identity.of(obj) if walk.identity is not None else None
    if obj_id is not None:
        out["id"] = obj_id
    out.update({k: v for k, v in fields.items() if v is not False})
    return out


def _cycle_or_depth(obj: object, depth: int, walk: _Walk) -> CapturedValue | None:
    """Back-reference on a cycle, marker at the depth limit, else None to proceed.

    The `seen` set is keyed on raw `id()`, and that is correct here: every object
    on the path is alive, held by our own frames, for the microseconds the walk
    takes. The id-reuse trap is about *durable* identity and is identity.py's
    problem, not this one.

    Emitting a back-reference rather than recursing gives structural sharing for
    free: a graph where one dict appears in fifty places is captured once and
    referenced forty-nine times.
    """
    if depth >= walk.policy.max_depth:
        return {"$": "depth", "type": type(obj).__name__}
    key = id(obj)
    if key in walk.seen:
        return {"$": "cycle", "id": walk.identity.of(obj) if walk.identity else None}
    walk.seen.add(key)
    return None


def _safe_len(obj: object) -> int:
    try:
        return len(obj)  # type: ignore[arg-type]
    except (TypeError, OverflowError):
        return -1  # a container that will not say how big it is


def _read_state(obj: object) -> dict[str, Any] | None:
    """Instance state, without ever invoking user code.

    `obj.__dict__` rather than `getattr`: properties and `__getattr__` live on the
    *type*, so an instance dict cannot reach them. That makes the no-user-code
    rule structural rather than a list of things to remember not to do.

    `__slots__` classes have no instance dict, so the slot descriptor is invoked
    off the type directly. `getattr` would work right up until an unset slot raises
    AttributeError and fires `__getattr__` -- user code, via the error path.

    Returns:
        Attribute mapping, or None if this object exposes no readable state.
    """
    type_dict = getattr(type(obj), "__dict__", {})
    slots = type_dict.get("__slots__")
    if slots is not None:
        out: dict[str, Any] = {}
        for name in (slots,) if isinstance(slots, str) else slots:
            descriptor = type_dict.get(name)
            if descriptor is None:
                continue
            try:
                out[name] = descriptor.__get__(obj, type(obj))
            except AttributeError:
                continue  # declared but never assigned
        return out
    try:
        instance = object.__getattribute__(obj, "__dict__")
    except AttributeError:
        return None
    return instance if isinstance(instance, dict) else None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

Handler = Callable[[object, int, _Walk], CapturedValue]

_HANDLERS: dict[type, Handler] = {
    type(None): _atom,
    bool: _atom,
    int: _atom,
    float: _atom,
    complex: _atom,
    str: _string,
    bytes: _string,
    bytearray: _string,
    list: _sequence,
    tuple: _sequence,
    set: _sequence,
    frozenset: _sequence,
    dict: _mapping,
}
"""Exact type -> handler.

Measured against the alternatives on typical locals (day 7):

    exact-type dict           113 ns/value
    isinstance chain          202 ns/value   (1.8x)
    functools.singledispatch  322 ns/value   (2.9x)

Beyond speed, a registry is what makes day 41's numpy/pandas support a plugin
rather than a rewrite: register a handler for `ndarray` and nothing else changes.
An `isinstance` chain would need a new branch in the hot path for every type
anyone ever cares about.
"""

_SUBCLASS_ORDER: tuple[tuple[type, Handler], ...] = (
    (str, _string),
    (bytes, _string),
    (bytearray, _string),
    (dict, _mapping),
    (list, _sequence),
    (tuple, _sequence),
    (set, _sequence),
    (frozenset, _sequence),
    (bool, _atom),
    (int, _atom),
    (float, _atom),
)
"""Checked in order for subclasses only. `bool` before `int` because it is one."""


def _handler_for(cls: type) -> Handler:
    """The handler for `cls`, memoised.

    An exact-type dict is O(1) but blind to subclasses: `class MyList(list)` is not
    `list`. The first sighting of a subclass walks `_SUBCLASS_ORDER` once and then
    caches the answer, so every later instance is a plain dict probe.

    The cache grows with the number of *classes* seen, which is bounded by the
    program's source rather than its data -- the same argument that makes day 4's
    code-object interning safe. A program synthesising unbounded classes at runtime
    would grow it; that is exotic, and noted rather than guarded against.

    Complexity: O(1) amortised.
    """
    handler = _HANDLERS.get(cls)
    if handler is not None:
        return handler
    for base, base_handler in _SUBCLASS_ORDER:
        if issubclass(cls, base):
            _HANDLERS[cls] = base_handler
            return base_handler
    _HANDLERS[cls] = _object
    return _object
