"""Proves the three capture invariants the whole recorder will rest on.

Day 7 promotes capture into `src/chronotrace/recorder/`. These tests exist now,
against the throwaway spike, because the *policy* is what day 7 inherits and a
policy nobody tested is a guess. Driven through a subprocess so `spikes/` stays
off the import graph of anything under `mypy --strict`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SPIKES = Path(__file__).parent.parent / "spikes"


def _run(code: str) -> str:
    """Execute `code` with spikes/ importable, in a fresh interpreter."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=SPIKES,
        timeout=300,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    return proc.stdout.strip()


def test_capture_never_invokes_user_code() -> None:
    """The invariant that separates observing from participating.

    If capture runs a `__repr__` or a property, the debugger has changed the
    program it is watching -- and can hide the very bug being hunted. stdlib's
    reprlib fails exactly this test, which is why it was rejected.
    """
    out = _run(
        "import hostile, spike_capture as sc;"
        "hostile.reset_sentinels();"
        "z = hostile.build_zoo();"
        "[sc.capture(v) for k, v in z.items() if not k.startswith('_')];"
        "print(hostile.EXPLODED, hostile.TOUCHED)"
    )
    assert out == "False False", f"capture invoked user code: {out}"


def test_reprlib_does_invoke_user_code() -> None:
    """Pins the reason stdlib was rejected.

    If a future Python makes reprlib safe, this test fails and we should delete
    our capture code and use it. A rejection with no expiry check is dogma.
    """
    out = _run(
        "import reprlib, hostile;"
        "hostile.reset_sentinels();"
        "reprlib.Repr().repr(hostile.ReprExplodes());"
        "print(hostile.EXPLODED)"
    )
    assert out == "True", "reprlib no longer calls user __repr__ -- reconsider ADR"


@pytest.mark.parametrize(
    "case",
    [
        "self_referential_list",
        "mutual_pair",
        "huge_list",
        "deep_dict",
        "repr_explodes",
        "property_side_effects",
        "fabricates_attributes",
        "generator",
        "open_file",
        "socket",
        "lock",
        "fake_array",
        "slotted",
        "weakref",
        "long_string",
    ],
)
def test_hostile_case_is_bounded_and_never_raises(case: str) -> None:
    """Every hostile input: terminates, stays in budget, does not raise.

    The byte ceiling is the real assertion. A capture that "succeeds" by
    emitting 4GB has not succeeded.
    """
    out = _run(
        "import json, hostile, spike_capture as sc;"
        f"v = hostile.build_zoo()[{case!r}];"
        "print(len(json.dumps(sc.capture(v), default=str)))"
    )
    assert int(out) < 4096, f"{case} captured {out} bytes -- over budget"


def test_truncation_is_visible_not_silent() -> None:
    """A lossy tool is fine. A lying one is not.

    Showing 100 of 10,000,000 items without saying so teaches the user the list
    has 100 items, and they debug the wrong thing.
    """
    out = _run(
        "import hostile, spike_capture as sc;"
        "c = sc.capture(hostile.build_zoo()['huge_list']);"
        "print(c['truncated'], c['len'], len(c['items']))"
    )
    assert out == "True 10000000 100"


def test_id_is_reused_after_gc() -> None:
    """The trap: id() is unique only among *live* objects.

    CPython reuses addresses. Using id() as durable identity across a recording
    means two different objects, minutes apart, share an identity -- so the UI
    would draw an "is the same object" badge between things that never coexisted.
    Proven here rather than asserted, because it sounds theoretical until you
    watch it happen.
    """
    out = _run(
        "import gc\n"
        "class T:\n"
        "    pass\n"
        "seen = set()\n"
        "collision = False\n"
        "for _ in range(10000):\n"
        "    o = T()\n"
        "    if id(o) in seen:\n"
        "        collision = True\n"
        "        break\n"
        "    seen.add(id(o))\n"
        "    del o\n"
        "    gc.collect()\n"
        "print(collision)"
    )
    assert out == "True", "id() did not collide -- the trap may not reproduce here"


def test_weak_identity_survives_id_reuse() -> None:
    """The fix: monotonic ids handed out on first sight, held weakly.

    Weak, so the recorder never extends a recorded object's lifetime. Extending
    it would change GC behaviour and could mask the refcount bug being debugged.
    """
    out = _run(
        "import gc, weakref, itertools\n"
        "class T:\n"
        "    pass\n"
        "ids = weakref.WeakKeyDictionary()\n"
        "counter = itertools.count()\n"
        "def oid(o):\n"
        "    if o not in ids:\n"
        "        ids[o] = next(counter)\n"
        "    return ids[o]\n"
        "assigned = []\n"
        "raw = []\n"
        "for _ in range(2000):\n"
        "    o = T()\n"
        "    assigned.append(oid(o))\n"
        "    raw.append(id(o))\n"
        "    del o\n"
        "    gc.collect()\n"
        "print(len(set(raw)) < len(raw), len(set(assigned)) == len(assigned))"
    )
    assert out == "True True", "raw ids must collide; assigned ids must not"


def test_capture_does_not_retain_the_object() -> None:
    """The recorder must never keep a recorded object alive.

    Retaining it changes when the program's finalisers run -- so the debugger
    would alter the timing of the very thing under observation.
    """
    out = _run(
        "import gc, weakref\n"
        "import spike_capture as sc\n"
        "class T:\n"
        "    def __init__(self):\n"
        "        self.data = [1, 2, 3]\n"
        "o = T()\n"
        "r = weakref.ref(o)\n"
        "captured = sc.capture(o)\n"
        "del o\n"
        "gc.collect()\n"
        "print(r() is None)"
    )
    assert out == "True", "capture retained the object -- it must hold no references"
