"""The replay-equivalence harness: the project's referee.

`check(fn)` runs a program under the recorder **and** an independent observer
(`truth.py`), writes a real `.chrono`, reconstructs at sampled instants, and compares
what was reconstructed against what actually happened (`compare.py`). It spans every
subsystem at once -- recorder, value pool, dedup, writer, keyframes, deltas, reader,
reconstructor -- and it is the only test in the project that can catch all of them being
wrong together.

Aligning an observation to an instant
-------------------------------------
The observer's callback runs before the recorder's for the same `LINE` event, so
`Observation.mark` is the `seq` the recorder is about to give that line. The recorder
then emits, inside one callback, that `LINE` plus a `VAR_WRITE` for every local that
changed. The instant to compare is therefore the **end of that group** -- `mark` plus its
trailing same-frame `VAR_WRITE`s -- because only there has the recorder finished stating
what it saw. Comparing at `mark` itself would report every fresh assignment as missing;
comparing at the next observation's `mark - 1` would run past a `CALL` into the callee.

That ordering is a CPython implementation detail, so it is **verified, not trusted**:
`_align` asserts that `events[mark]` really is a `LINE` at the observed code and line. If
a future interpreter fires the tools the other way round, the harness says so instead of
silently comparing the wrong instants.

Sampling: deterministic boundaries, random middle
-------------------------------------------------
Truth capture is far more expensive than recording (every local, every line, no change
detection), so large programs are sampled. Boundaries get **deterministic** coverage --
keyframe edges, frame entries and exits, raises, yields and resumes -- because that is
where two mechanisms hand off to each other and where off-by-ones live: reconstruction
switches from "decode a keyframe" to "replay deltas" exactly at a keyframe edge, and the
frame set changes exactly at a call or return. A bug there sampled randomly shows up as a
*flaky* test, which is worse than no test. The middle of a straight-line run is
homogeneous, so random coverage of it is a fair trade, and the seed is fixed so a failure
reproduces.
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import CodeType

from chronotrace.reconstruct import KeyframeReconstructor, ValueResolver
from chronotrace.recorder import Event, EventKind, MemorySink, Recorder
from chronotrace.recorder.capture import DEFAULT_POLICY, CapturePolicy
from chronotrace.recorder.redact import Redactor
from chronotrace.recorder.scope import Scope
from chronotrace.store import ChronoReader, ChronoWriter

from .compare import Mismatch, compare
from .truth import Observation, TruthObserver

__all__ = ["AlignmentError", "Mismatch", "Recording", "check", "record"]

BOUNDARY_KINDS = frozenset(
    {
        EventKind.CALL,
        EventKind.RETURN,
        EventKind.UNWIND,
        EventKind.RAISE,
        EventKind.EXCEPTION_HANDLED,
        EventKind.YIELD,
        EventKind.RESUME,
    }
)
"""Instants where two mechanisms hand off, and so are always sampled."""


class AlignmentError(AssertionError):
    """The observer and the recorder disagree about which instant is which.

    Not a mismatch -- a broken harness. Raised loudly rather than folded into the
    results, because a misaligned comparison would report a flood of nonsense.
    """


@dataclass(frozen=True, slots=True)
class Recording:
    """One program, recorded and observed at the same time."""

    reader: ChronoReader
    instants: list[tuple[int, Observation]]
    names: dict[int, str]
    codes: dict[int, CodeType]
    interval: int


def record(
    fn: Callable[[], object],
    scope: Scope,
    *,
    policy: CapturePolicy = DEFAULT_POLICY,
    redact: Redactor | None = None,
    block_events: int = 512,
    keyframe_interval: int = 64,
) -> Recording:
    """Run `fn` under the recorder and the observer, into a real `.chrono`.

    Small blocks and a short keyframe interval by default, so even a 50-event program
    crosses several keyframe and block boundaries -- the places the harness most wants to
    look. Production defaults would give a tiny program one keyframe and test nothing.

    Raises:
        AlignmentError: the observer and recorder disagree about instants.
    """
    sink = MemorySink()
    recorder = Recorder(sink, scope=scope, capture_values=True, policy=policy, redact=redact)
    with TruthObserver(sink.events, scope, policy=policy, redact=redact) as truth, recorder:
        fn()
    reader = _write(sink.events, recorder, block_events, keyframe_interval)
    return Recording(
        reader=reader,
        instants=_align(sink.events, truth.observations, recorder),
        names=dict(enumerate(recorder.names)),
        codes=dict(enumerate(recorder.codes)),
        interval=keyframe_interval,
    )


def check(
    fn: Callable[[], object],
    scope: Scope,
    *,
    limit: int | None = None,
    seed: int = 0,
    **kwargs: object,
) -> list[Mismatch]:
    """Every way `fn`'s reconstruction disagrees with what really happened.

    Args:
        fn: the program to record. Called once.
        scope: which files count as the program.
        limit: sample at most this many instants. None checks every one.
        seed: fixes the random half of the sample, so a failure reproduces.
        **kwargs: passed to `record` (policy, redact, block sizes).

    Returns:
        Mismatches across every sampled instant. **Empty is the only acceptable result.**

    Complexity: O(sampled instants x (keyframe interval + bindings)).
    """
    recording = record(fn, scope, **kwargs)  # type: ignore[arg-type]
    reconstructor = KeyframeReconstructor(recording.reader)
    resolver = ValueResolver(recording.reader)
    out: list[Mismatch] = []
    for seq, observation in _sample(recording, limit, seed):
        state = reconstructor.reconstruct(seq)
        values = {ref: resolver.resolve(ref) for f in state.frames for ref in f.bindings.values()}
        out.extend(
            compare(
                state,
                observation,
                values,
                recording.names,
                recording.codes,
                _provenance(recording, seq),
            )
        )
    return out


def _write(
    events: list[Event], recorder: Recorder, block_events: int, keyframe_interval: int
) -> ChronoReader:
    buf = io.BytesIO()
    writer = ChronoWriter(buf, block_events=block_events, keyframe_interval=keyframe_interval)
    for captured in recorder.values:
        writer.add_value(captured)
    for event in events:
        writer.add(event)
    writer.close()
    return ChronoReader.from_bytes(buf.getvalue())


def _align(
    events: list[Event], observations: Iterable[Observation], recorder: Recorder
) -> list[tuple[int, Observation]]:
    """Pair each observation with the `seq` that completes its event group.

    See the module docstring. The alignment claim is checked, not assumed.
    """
    codes = dict(enumerate(recorder.codes))
    out = []
    for observation in observations:
        mark = observation.mark
        if mark >= len(events):
            continue  # the recorder stopped mid-line (it exits before the observer)
        anchor = events[mark]
        if (
            anchor.kind is not EventKind.LINE
            or anchor.lineno != observation.lineno
            or codes.get(anchor.code_id) is not observation.code
        ):
            raise AlignmentError(
                f"observation at mark {mark} expected a LINE at "
                f"{observation.code.co_qualname}:{observation.lineno}, but the recorder's "
                f"event {mark} is {anchor.kind.name} at line {anchor.lineno}. The observer "
                f"is no longer running before the recorder -- the harness needs fixing, "
                f"not the code under test."
            )
        out.append((_group_end(events, mark), observation))
    return out


def _group_end(events: list[Event], mark: int) -> int:
    """The last `seq` the recorder emits for one line: the LINE plus its own VAR_WRITEs."""
    end, frame = mark, events[mark].frame_id
    while (
        end + 1 < len(events)
        and events[end + 1].kind is EventKind.VAR_WRITE
        and events[end + 1].frame_id == frame
    ):
        end += 1
    return end


def _sample(recording: Recording, limit: int | None, seed: int) -> list[tuple[int, Observation]]:
    """Every boundary instant, plus a fixed-seed random selection of the rest."""
    instants = recording.instants
    if limit is None or len(instants) <= limit:
        return instants
    events = recording.reader[0 : len(recording.reader)]
    boundary = [i for i in instants if _is_boundary(i[0], events, recording.interval)]  # type: ignore[arg-type]
    middle = [i for i in instants if i not in boundary]
    take = max(0, limit - len(boundary))
    chosen = boundary + random.Random(seed).sample(middle, min(take, len(middle)))  # noqa: S311
    return sorted(chosen, key=lambda i: i[0])


def _is_boundary(seq: int, events: list[Event], interval: int) -> bool:
    """A keyframe edge, or an instant next to a frame or exception transition."""
    if seq % interval <= 1 or (seq + 1) % interval == 0:
        return True
    window = events[max(0, seq - 1) : seq + 2]
    return any(e.kind in BOUNDARY_KINDS for e in window)


def _provenance(recording: Recording, seq: int) -> str:
    """How reconstruction reached this instant -- quoted into every mismatch."""
    keyframe = recording.reader.nearest_keyframe_at_or_before(seq)
    if keyframe is None:
        return f"reconstructed from seq 0 (no keyframe survived at or before {seq})"
    deltas = recording.reader.deltas_between(keyframe.seq + 1, seq)
    return (
        f"reconstructed from keyframe {keyframe.seq} "
        f"({seq - keyframe.seq} events back), replaying {len(deltas)} deltas"
    )
