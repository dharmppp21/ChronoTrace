"""Object graphs that break naive capture. Promoted from `spikes/hostile.py`.

Every entry is something a real program contains. Cycles come from parent/child
links, huge lists from data pipelines, sockets and locks from every server, and a
raising `__repr__` from any half-constructed object.

Two rules shaped this file:

* **No third-party dependencies.** The numpy-like case is faked with an object
  exposing `shape`/`dtype`/`nbytes`, because what capture must handle is the
  *shape of the problem* -- an object claiming a huge buffer -- not numpy. Buying
  a dependency for one fixture is a bad trade.
* **Side effects are observable.** `EXPLODED` and `TOUCHED` are module flags. If
  capture ever invokes user code, a test proves it rather than inferring it.

`wide_and_deep` is new on day 7 and is the one the spike's zoo lacked. Its absence
hid a real hole: depth and item limits bound each dimension, but a 20x20x20x20x20
list -- an ordinary shape -- measured at 26 seconds and 416 MB under the day 3
policy. Every fixture here is a bug that got through.
"""

from __future__ import annotations

import socket
import threading
import weakref
from pathlib import Path
from typing import Any

EXPLODED = False
TOUCHED = False


def reset_sentinels() -> None:
    """Clear the side-effect flags before a test that asserts they stay clear."""
    global EXPLODED, TOUCHED
    EXPLODED = False
    TOUCHED = False


class ReprExplodes:
    """A half-constructed object. Its `__repr__` raises, as real ones do."""

    def __repr__(self) -> str:
        global EXPLODED
        EXPLODED = True
        raise RuntimeError("__repr__ raised during capture")


class PropertyHasSideEffects:
    """A property that mutates state when read.

    Why capture may never use `getattr`: reading a property *runs code*, and a
    debugger that runs the program's code while observing it is no longer
    observing -- it is participating.
    """

    def __init__(self) -> None:
        self.reads = 0

    @property
    def counter(self) -> int:
        global TOUCHED
        TOUCHED = True
        self.reads += 1
        return self.reads


class FabricatesAttributes:
    """`__getattr__` invents any attribute asked for -- an infinite surface."""

    def __getattr__(self, name: str) -> Any:
        global TOUCHED
        TOUCHED = True
        return FabricatesAttributes()


class LiesAboutItsClass:
    """Overrides `__class__`. `isinstance` believes it; `type()` does not.

    Capture dispatches on `type(obj)`, never `isinstance(obj, ...)`, which is what
    makes this merely odd rather than a hijack: a class claiming to be a `dict`
    cannot route itself into the mapping handler and have `.items()` called on it.
    """

    def __getattribute__(self, name: str) -> Any:
        if name == "__class__":
            global TOUCHED
            TOUCHED = True
            return dict
        return object.__getattribute__(self, name)


class Slotted:
    """No `__dict__` at all. Capture cannot assume instance dicts exist."""

    __slots__ = ("never_set", "x", "y")

    def __init__(self) -> None:
        self.x = 1
        self.y = "two"


class Node:
    """Mutual reference: `a.peer.peer is a`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.peer: Node | None = None


class FakeArray:
    """Stands in for numpy: an object claiming a buffer too big to capture."""

    def __init__(self, nbytes: int = 4_000_000_000) -> None:
        self.shape = (1_000_000, 500)
        self.dtype = "float64"
        self.nbytes = nbytes


def _self_referential_list() -> list[Any]:
    a: list[Any] = [1, 2, 3]
    a.append(a)
    return a


def _mutual_pair() -> Node:
    a, b = Node("a"), Node("b")
    a.peer, b.peer = b, a
    return a


def _deep_dict(depth: int = 10_000) -> dict[str, Any]:
    root: dict[str, Any] = {}
    cur = root
    for _ in range(depth):
        nxt: dict[str, Any] = {}
        cur["next"] = nxt
        cur = nxt
    return root


def _wide_and_deep(width: int = 20, depth: int = 5) -> Any:
    """The shape day 3's zoo missed. 20**5 = 3.2M nodes if left unbounded."""
    if depth == 0:
        return list(range(width))
    return [_wide_and_deep(width, depth - 1) for _ in range(width)]


def _generator() -> Any:
    def gen() -> Any:
        yield 1
        yield 2

    return gen()


def build_zoo() -> dict[str, Any]:
    """Every hostile case, built fresh.

    Returns:
        name -> value. Names starting with "_" are support, not cases.

    Complexity: O(depth + width). ~0.5s and real memory -- the 10M list and the
    10k-deep dict dominate. Build once per test session.
    """
    target = Node("weakref-target")
    return {
        "self_referential_list": _self_referential_list(),
        "mutual_pair": _mutual_pair(),
        "huge_list": list(range(10_000_000)),
        "deep_dict": _deep_dict(),
        "wide_and_deep": _wide_and_deep(),
        "repr_explodes": ReprExplodes(),
        "property_side_effects": PropertyHasSideEffects(),
        "fabricates_attributes": FabricatesAttributes(),
        "lies_about_its_class": LiesAboutItsClass(),
        "generator": _generator(),
        "open_file": Path(__file__).open(encoding="utf-8"),
        "socket": socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        "lock": threading.Lock(),
        "fake_array": FakeArray(),
        "slotted": Slotted(),
        "weakref": weakref.ref(target),
        "_weakref_target": target,
        "long_string": "x" * 5_000_000,
        "nested_mixed": {"a": [1, {"b": (2, 3)}], "c": {"d": [4, 5]}},
    }


def build_typical() -> dict[str, Any]:
    """The values 99% of captures see. Cost here is what users feel."""
    return {
        "int": 42,
        "float": 3.14159,
        "str": "hello world",
        "bool": True,
        "none": None,
        "small_list": [1, 2, 3, 4, 5],
        "small_dict": {"id": 1, "name": "widget", "price": 9.99},
        "tuple": (1, "two", 3.0),
        "list_of_dicts": [{"id": i, "v": i * 2} for i in range(20)],
    }
