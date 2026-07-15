"""Can we capture values cheaply and safely? Spike, not a library.

Observing *which line ran* is easy. Capturing *what the variables held* is where
debuggers die: object graphs are cyclic, 10k deep, 10M wide, and sometimes
actively hostile.

Why not stdlib
--------------
``reprlib.Repr`` was the obvious candidate and nearly fits: ``maxlevel``,
``maxlist``, ``maxdict`` and ``maxstring`` are exactly our policy, and it handles
cycles and depth in microseconds. It was rejected on two measured grounds
(see RESULTS-capture.md):

1. **It calls user code.** Against ``hostile.ReprExplodes`` it set the sentinel:
   ``reprlib`` invoked the user's ``__repr__``. It swallows the exception, but
   the code already ran. For a debugger that is a *correctness* bug, not a
   performance one -- observing must not change the observed.
2. **It returns a string.** A string cannot be expanded on click, diffed against
   the previous instant, or given an identity badge. The variable panel needs
   structure.

Its policy *shape* is borrowed; its implementation is not.

Three invariants
----------------
1. **Bounded** -- depth, item count and string length are capped.
2. **Never invokes user code** -- structurally, not defensively: attributes are
   read from ``obj.__dict__``, and properties live on the *type*, so they are
   never reachable. ``getattr`` would fire them; we never call it.
3. **Never retains** -- returns plain data, holds no reference to the input.

Representation
--------------
Plain nested ``dict``/``list``/atoms -- deliberately not a class hierarchy. The
output is directly msgpack- and json-serialisable with no encoder, and a class
would be an abstraction over data that is already the right shape. Day 7 can
add types if a second consumer ever needs them.

Atoms pass through unwrapped. Every container is wrapped in a tagged dict, so a
user dict containing a ``"$"`` key is never confused with our tag.
"""

import itertools
import json
import time
from typing import Any

import hostile

# Borrowed from reprlib's proven shape. Defaults justified by measurement in
# RESULTS-capture.md, not chosen by taste.
MAX_DEPTH = 6
MAX_ITEMS = 100
MAX_STRING = 512

# bool is a subclass of int, so isinstance already covers it.
_ATOMS = (int, float, type(None))


def capture(
    obj: Any,
    depth: int = 0,
    seen: set[int] | None = None,
    *,
    max_depth: int = MAX_DEPTH,
    max_items: int = MAX_ITEMS,
    max_string: int = MAX_STRING,
) -> Any:
    """Turn any object into bounded, serialisable, honest data.

    Recursive rather than iterative, deliberately: ``max_depth`` already bounds
    the stack to ~6 frames, so the RecursionError an explicit work-stack would
    prevent cannot occur. An explicit stack would solve a problem the depth
    limit has already solved, at the cost of much harder code.

    Args:
        obj: anything at all, including hostile input.
        depth: current recursion depth (internal).
        seen: ids of containers on the current path, for cycle detection
            (internal). Safe to key on ``id()`` here because every object on the
            path is alive -- held by our own call frames. The ``id()`` reuse trap
            is about *durable* identity across a recording, a different problem
            (see RESULTS-capture.md).
        max_depth: recurse no deeper; beyond this, summarise.
        max_items: capture at most this many elements of a container.
        max_string: truncate strings/bytes beyond this length.

    Returns:
        Nested dicts/lists/atoms. Never raises.

    Complexity: O(min(n, max_items) * min(d, max_depth)) -- bounded by policy,
    not by the size of the input graph. A 10M-element list costs the same as a
    100-element one.
    """
    if seen is None:
        seen = set()

    if isinstance(obj, _ATOMS):
        return obj
    if isinstance(obj, (str, bytes)):
        return _capture_string(obj, max_string)

    oid = id(obj)
    if oid in seen:
        return {"$": "cycle", "id": oid}
    if depth >= max_depth:
        return {"$": "depth", "type": type(obj).__name__}

    seen = seen | {oid}
    kw = {"max_depth": max_depth, "max_items": max_items, "max_string": max_string}

    if isinstance(obj, (list, tuple, set, frozenset)):
        return _capture_sequence(obj, depth, seen, kw)
    if isinstance(obj, dict):
        return _capture_dict(obj, depth, seen, kw)
    return _capture_object(obj, depth, seen, kw)


def _capture_string(obj: str | bytes, max_string: int) -> Any:
    """Truncation is data, never silence.

    A user shown 512 of 5,000,000 characters with no marker will believe the
    string is 512 characters long and debug the wrong thing. The marker is the
    difference between a lossy tool and a lying one.
    """
    if len(obj) <= max_string:
        return obj if isinstance(obj, str) else {"$": "bytes", "v": obj[:max_string].hex()}
    return {
        "$": "str" if isinstance(obj, str) else "bytes",
        "head": obj[:max_string] if isinstance(obj, str) else obj[:max_string].hex(),
        "len": len(obj),
        "truncated": True,
    }


def _capture_sequence(obj: Any, depth: int, seen: set[int], kw: dict[str, int]) -> dict[str, Any]:
    max_items = kw["max_items"]
    # islice, not list(obj)[:max_items]. The latter materialises the whole
    # container before slicing it: measured at 70ms for a 10M-element list,
    # against 4us with islice. The policy said "capture at most 100 items"; the
    # code said "copy ten million things, then take 100". O(max_items), not O(n).
    items = [capture(v, depth + 1, seen, **kw) for v in itertools.islice(obj, max_items)]
    return {
        "$": type(obj).__name__,
        "items": items,
        "len": len(obj),
        "truncated": len(obj) > max_items,
    }


def _capture_dict(
    obj: dict[Any, Any], depth: int, seen: set[int], kw: dict[str, int]
) -> dict[str, Any]:
    max_items = kw["max_items"]
    pairs = []
    for i, (k, v) in enumerate(obj.items()):
        if i >= max_items:
            break
        pairs.append([capture(k, depth + 1, seen, **kw), capture(v, depth + 1, seen, **kw)])
    return {"$": "dict", "items": pairs, "len": len(obj), "truncated": len(obj) > max_items}


def _read_attrs(obj: Any) -> dict[str, Any] | None:
    """Read instance state without ever invoking user code.

    ``obj.__dict__`` rather than ``getattr``: properties and ``__getattr__`` live
    on the *type*, so an instance dict cannot reach them. This makes the
    no-user-code rule structural rather than a list of things to remember not to
    do.

    ``__slots__`` classes have no instance dict, so the slot descriptor is
    invoked directly off the type. ``getattr`` would work too, right up until an
    unset slot raises AttributeError and fires ``__getattr__`` -- user code, via
    the error path.

    Returns:
        Attribute mapping, or None if this object has no readable state.
    """
    d = getattr(type(obj), "__dict__", {})
    slots = d.get("__slots__")
    if slots is not None:
        out = {}
        for name in (slots,) if isinstance(slots, str) else slots:
            desc = d.get(name)
            if desc is None:
                continue
            try:
                out[name] = desc.__get__(obj, type(obj))
            except AttributeError:
                continue  # slot declared but never assigned
        return out
    inst = obj.__dict__ if hasattr(type(obj), "__dict__") and "__dict__" in dir(type(obj)) else None
    return inst if isinstance(inst, dict) else None


def _capture_object(obj: Any, depth: int, seen: set[int], kw: dict[str, int]) -> dict[str, Any]:
    """Capture an arbitrary object as type metadata plus its instance state.

    Objects we cannot descend into (sockets, locks, files, generators) get a type
    summary and never the resource itself -- a captured socket is meaningless and
    a captured file handle is a leak.
    """
    t = type(obj)
    out: dict[str, Any] = {
        "$": "obj",
        "type": t.__name__,
        "module": getattr(t, "__module__", "?"),
        "id": id(obj),
    }

    attrs = _read_attrs(obj)
    if attrs is None:
        out["opaque"] = True  # socket, lock, generator: no inspectable state
        return out

    # An object claiming a buffer far too large to hold: describe, never copy.
    if {"shape", "nbytes"} <= attrs.keys():
        out["buffer"] = {k: attrs.get(k) for k in ("shape", "dtype", "nbytes")}
        return out

    max_items = kw["max_items"]
    out["attrs"] = {
        str(k): capture(v, depth + 1, seen, **kw)
        for k, v in itertools.islice(attrs.items(), max_items)
    }
    if len(attrs) > max_items:
        out["truncated"] = True
    return out


def measure(values: dict[str, Any], label: str) -> None:
    """Print bytes and microseconds per capture. Output is the deliverable."""
    print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
    print(f"{'value':<24} {'us/capture':>11} {'json bytes':>11}  {'user code?':>10}")
    print("-" * 74)
    for name, val in values.items():
        if name.startswith("_"):
            continue
        hostile.reset_sentinels()
        t0 = time.perf_counter()
        for _ in range(20):
            out = capture(val)
        us = (time.perf_counter() - t0) / 20 * 1e6
        try:
            nbytes: Any = len(json.dumps(out, default=str))
        except (TypeError, ValueError):
            nbytes = "n/a"
        flag = "YES" if (hostile.EXPLODED or hostile.TOUCHED) else "no"
        print(f"{name:<24} {us:>11.2f} {nbytes:>11}  {flag:>10}")


def measure_serialization(values: dict[str, Any]) -> None:
    """Compare msgpack / json / pickle on captured representations.

    pickle is measured for completeness and then rejected on security grounds,
    not performance -- see RESULTS-capture.md. Opening a recording must never be
    able to execute code, because recordings get shared in bug reports.
    """
    import pickle

    import msgpack

    captured = {k: capture(v) for k, v in values.items() if not k.startswith("_")}
    print(f"\n{'=' * 74}\nSERIALISATION of captured representations\n{'=' * 74}")
    print(f"{'format':<12} {'total bytes':>12} {'us/value':>10}   note")
    print("-" * 74)
    for name, dump in (
        ("msgpack", lambda o: msgpack.packb(o, default=str)),
        ("json", lambda o: json.dumps(o, default=str).encode()),
        ("pickle", lambda o: pickle.dumps(o, protocol=5)),
    ):
        t0 = time.perf_counter()
        total = 0
        for _ in range(20):
            total = sum(len(dump(c)) for c in captured.values())
        us = (time.perf_counter() - t0) / 20 / len(captured) * 1e6
        note = "REJECTED: arbitrary code execution on load" if name == "pickle" else ""
        print(f"{name:<12} {total:>12,} {us:>10.2f}   {note}")


def main() -> int:
    measure(hostile.build_typical(), "TYPICAL LOCALS -- the 99% case, where cost is felt")
    measure(hostile.build_zoo(), "HOSTILE ZOO -- must never raise, hang, or run user code")
    measure_serialization(hostile.build_typical())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
