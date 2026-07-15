"""Pins the event vocabulary and the seq contract.

Seven layers depend on these types. The tests here are less about catching bugs in
30 lines of dataclass than about making the *contract* executable, so that a
future change which quietly breaks it fails loudly instead of surfacing as a
wrong answer in the UI six weeks later.
"""

from __future__ import annotations

import itertools
import threading
import weakref

import pytest

from chronotrace.recorder import Event, EventKind, MemorySink
from chronotrace.recorder.values import ValuePool


def _event(seq: int, kind: EventKind = EventKind.LINE, **kw: object) -> Event:
    base: dict[str, object] = {
        "timestamp_ns": 1_000 + seq,
        "thread_id": 1,
        "frame_id": 7,
        "code_id": 3,
        "lineno": 42,
    }
    base.update(kw)
    return Event(seq=seq, kind=kind, **base)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", list(EventKind))
def test_every_kind_round_trips(kind: EventKind) -> None:
    ev = _event(1, kind)
    assert ev.kind is kind
    assert ev.seq == 1
    assert ev.lineno == 42


def test_events_are_frozen() -> None:
    """An event is a historical fact. Nothing downstream may edit the past."""
    ev = _event(1)
    with pytest.raises(AttributeError):
        ev.seq = 2  # type: ignore[misc]


def test_event_kinds_are_ints_and_stable() -> None:
    """Kind values are written to disk and compared in SQL.

    Pinned literally: renumbering them silently reinterprets every recording ever
    made, and nothing would fail until a user saw a CALL rendered as a RAISE.
    """
    assert EventKind.LINE.value == 1
    assert EventKind.VAR_WRITE.value == 9
    assert isinstance(EventKind.LINE, int)


def test_optional_fields_default_to_absent() -> None:
    """Only VAR_WRITE carries a name and value; every other kind leaves them None."""
    assert _event(1).name_id is None
    assert _event(1).value_ref is None


def test_seq_is_strictly_increasing_across_threads() -> None:
    """The seq contract under concurrency.

    `itertools.count().__next__` is atomic under CPython's GIL, which is what
    makes this pass without a lock. PEP 703's free-threaded build removes that
    guarantee -- this test is the tripwire. If it ever goes red on a free-threaded
    interpreter, day 5's counter needs real synchronisation, and the cost of that
    lock lands squarely in the hot path.
    """
    counter = itertools.count()
    sink = MemorySink()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()  # maximise contention rather than hoping for it
        for _ in range(2_000):
            sink.emit(_event(next(counter)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = sorted(e.seq for e in sink.events)
    assert len(seqs) == 16_000
    assert seqs == list(range(16_000)), "seq must be unique and dense"


def test_memory_sink_does_not_retain_user_objects() -> None:
    """The sink holds captured data, never the program's live objects.

    Retaining them would change when the program's finalisers run -- the debugger
    altering the timing of the thing it is observing. The pool stores captured
    representations (plain data), so dropping the original must free it.
    """

    class Tracked:
        def __init__(self) -> None:
            self.payload = [1, 2, 3]

    pool = ValuePool()
    sink = MemorySink()
    obj = Tracked()
    ref = weakref.ref(obj)

    # what the recorder will do on day 7: capture to plain data, store the ref
    value_ref = pool.add({"$": "obj", "type": "Tracked", "attrs": {"payload": [1, 2, 3]}})
    sink.emit(_event(1, EventKind.VAR_WRITE, name_id=0, value_ref=value_ref))

    del obj
    assert ref() is None, "sink or pool retained the user's object"


def test_memory_sink_close_is_idempotent() -> None:
    """The recorder closes on both the normal and the exception path.

    A double close must not become a second failure on top of the first.
    """
    sink = MemorySink()
    sink.close()
    sink.close()
    assert sink.events == []


def test_value_pool_round_trips() -> None:
    pool = ValuePool()
    ref = pool.add({"$": "list", "items": [1, 2]})
    assert pool.resolve(ref) == {"$": "list", "items": [1, 2]}


def test_value_pool_rejects_unknown_ref() -> None:
    from chronotrace.recorder.values import ValueRef

    with pytest.raises(IndexError):
        ValuePool().resolve(ValueRef(99))
