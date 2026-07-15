"""The adversarial value zoo: object graphs that break naive capture.

Every entry here is something a real program actually contains. None of it is
contrived for its own sake -- cycles come from parent/child links, huge lists
come from data pipelines, sockets and locks live in every server, and a
``__repr__`` that raises is what a half-constructed object does.

A debugger meets all of it, in the user's hot path, and must not crash, hang,
allocate unboundedly, or change the program it is observing.

Two rules shaped this file:

* **No third-party dependencies.** The numpy-like case is faked with an object
  exposing ``shape``/``dtype``/``nbytes``, because what capture must handle is
  the *shape of the problem* (an object claiming a huge buffer), not numpy
  specifically. Adding numpy to measure that would be a dependency bought for a
  single fixture.
* **Side-effect sentinels are observable.** ``EXPLODED`` and ``TOUCHED`` are
  module-level flags. If capture ever invokes user code, a test can prove it
  rather than infer it.

Graduates to ``tests/fixtures/hostile.py`` on day 7 when capture becomes real.
"""

import socket
import threading
import weakref
from pathlib import Path
from typing import Any

# Set by the sentinels below if capture ever invokes user code. A test asserts
# these stay False -- the "never call user code" rule made checkable.
EXPLODED = False
TOUCHED = False


def reset_sentinels() -> None:
    global EXPLODED, TOUCHED
    EXPLODED = False
    TOUCHED = False


class ReprExplodes:
    """A half-constructed object. Its ``__repr__`` raises, as real ones do."""

    def __repr__(self) -> str:
        global EXPLODED
        EXPLODED = True
        raise RuntimeError("__repr__ raised during capture")


class PropertyHasSideEffects:
    """A property that mutates state when read.

    This is why capture may not use ``getattr``: reading a property *runs code*,
    and a debugger that runs the program's code while observing it is no longer
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
    """``__getattr__`` invents any attribute asked for -- an infinite surface."""

    def __getattr__(self, name: str) -> Any:
        global TOUCHED
        TOUCHED = True
        return FabricatesAttributes()


class Slotted:
    """No ``__dict__`` at all. Capture cannot assume instance dicts exist."""

    __slots__ = ("x", "y")

    def __init__(self) -> None:
        self.x = 1
        self.y = "two"


class Node:
    """Mutual reference: ``a.peer.peer is a``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.peer: Node | None = None


class FakeArray:
    """Stands in for numpy: an object claiming a buffer far too big to capture.

    Capture must record shape/dtype and refuse the payload. The point is not
    numpy -- it is any object whose useful description is its metadata.
    """

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


def _generator() -> Any:
    def gen() -> Any:
        yield 1
        yield 2

    return gen()


def build_zoo() -> dict[str, Any]:
    """Every hostile case, built fresh.

    Returns:
        name -> value. Callers must not mutate the values.

    Complexity: O(depth + width) -- dominated by the 10M list and the depth-10k
    dict, so this takes ~0.3s and real memory. Build once per process.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target = Node("weakref-target")
    return {
        "self_referential_list": _self_referential_list(),
        "mutual_pair": _mutual_pair(),
        "huge_list": list(range(10_000_000)),
        "deep_dict": _deep_dict(),
        "repr_explodes": ReprExplodes(),
        "property_side_effects": PropertyHasSideEffects(),
        "fabricates_attributes": FabricatesAttributes(),
        "generator": _generator(),
        "open_file": Path(__file__).open(encoding="utf-8"),
        "socket": sock,
        "lock": threading.Lock(),
        "fake_array": FakeArray(),
        "slotted": Slotted(),
        "weakref": weakref.ref(target),
        "_weakref_target": target,  # keeps the weakref alive; underscore = not a case
        "long_string": "x" * 5_000_000,
        "nested_mixed": {"a": [1, {"b": (2, 3)}], "c": {"d": [4, 5]}},
    }


# Typical locals -- the common case the policy must be cheap for, not just safe.
def build_typical() -> dict[str, Any]:
    """The values 99% of captures actually see. Cost here is what users feel."""
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
