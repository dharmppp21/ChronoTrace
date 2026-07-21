"""Debugger stepping -- forward and backward -- as a search over events.

This is the feature the project exists for: every command a developer already knows
(`step`, `next`, `finish`, `continue`) plus its mirror image in time. The mirror is the
whole product, so the definitions matter more than the code, and they are written down
first.

The four operations, each defined as the mirror of its forward twin
-------------------------------------------------------------------
Let `cur` be the frame that executed the event at `seq` (`ProgramState.current_frame_id`),
and let a **stop instant** be a `LINE` event -- the instants a debugger pauses on.

`step` -- the next/previous stop instant in **any** frame. Forward this is "step into":
    it lands on a callee's first line. Backward it lands on the last line of the call
    that just finished, which is the same thing seen from the other end of time.
`step_over` -- the next/previous stop instant **in `cur`**. Nested calls run to
    completion going forward, and completed nested calls are skipped going back.
`step_out` -- forward, the instant `cur` exits (`RETURN`/`UNWIND`, i.e. "finish");
    backward, the instant `cur` was entered (`CALL`).
`seek` -- the next/previous instant satisfying a predicate: "continue", and
    "reverse-continue".

`step_out`'s two directions look for different events, and that is not two code paths
leaking: a frame's two ends genuinely *are* different events. Everything else differs
only by the sign of the scan.

Why a `seq` search, and not a state walk
----------------------------------------
The tempting implementation is to invert one delta at a time until the state "is at" a
line event. It is both slower and more fragile.

**Slower**, by the measured shape of a real recording: the previous stop instant *in the
current frame* sits a median of 1 event back but up to **280,993** events back -- one
`next` over a call made from a module-level frame (benchmarks/RESULTS.md). A state walk
would invert a quarter-million deltas and materialise a quarter-million intermediate
states, every one of them discarded, to answer a question that never needed a state at
all. The search reads events; state is built **once**, at the destination.

**More fragile**, which matters more: the stop condition ("is this a line in frame F?")
is a property of the *event*, so a state walk must consult the event stream anyway --
in lockstep with inversion, sharing an index. Any drift between the two walks (has the
delta at `seq` been undone yet, or not?) produces a state that is wrong but plausible,
the one failure a debugger cannot survive. Separating **where to stop** (a pure event
query, which cannot corrupt state) from **what the state is there** (one
`reconstruct`, already proven against the day-20 oracle) means backward stepping adds
no new way to be silently wrong.

So this module returns a `seq` and nothing else. It never touches `ProgramState`.

Why the destination is reconstructed, not reached by inverting deltas
---------------------------------------------------------------------
ADR-0006 §3 decided a backward step should invert the delta at `seq` for an O(1) step,
assuming the delta replay dominates. Measurement (day 21, benchmarks/RESULTS.md) says it
does not, and the reason is structural: **the control-flow overlay is not invertible.**
Deltas carry `old_ref` precisely so bindings can be undone, but an event carries no
*previous* `lineno`, so each frame's current line has to be re-derived from the keyframe
however the bindings arrived. That half is 487 µs of a 718 µs step; inversion's ceiling is
32% of an operation already 11x inside a frame budget.

Buying ~230 µs nobody can perceive, at the price of a second incremental state machine --
exactly the silently-drifting cached state ADR-0006 §4 calls unsurvivable -- is a bad
trade. So a backward step reconstructs at its destination like every other jump, through
the one path already proven equal to the day-20 oracle. Day 16's `old_ref` is not wasted:
it is what makes `invert` exist for the day-22 replay-equivalence harness, and it is the
lever to pull if the overlay is ever promoted into the keyframe (ADR-0006's own reversal
trigger, now tripped).

Generators: `step_back` lands in the consumer, and that is correct
------------------------------------------------------------------
`step`/`step_back` follow **execution order**, which for a generator alternates between
producer and consumer. Stepping back from a consumer's line just after a `yield` lands on
the generator's last executed line -- you travel back *into* a frame that a call-stack
mental model says is not below you. Stepping back from a generator's first line after a
`RESUME` lands in the **consumer**, wherever `next()` was called, which may be a different
function each time.

Both are honest reports of what actually ran, and both surprise. The rule for users:
**`step_over`/`step_over_back` stay in one frame** (they filter on `frame_id`), so they
are the operations that behave the way a stack model expects. The same filter is why
`step_over_back` under `asyncio` skips other tasks' events for free, and why under
recursion it stays in the *invocation* the user is looking at rather than jumping to
another call of the same function -- `frame_id` is per-frame, `code_id` is not.

Boundaries are values, never exceptions
---------------------------------------
Running off either end returns an `Edge`, because "you are at the beginning of the
recording" is an ordinary answer the UI renders, not an error it catches. An exception is
reserved for a `seq` that was never a valid instant -- a caller bug, not a boundary.
`Edge.LOST_TAIL` is distinguished from `Edge.END` on purpose: the end of a crash-truncated
recording is not the end of the program, and a debugger that conflates them lies.
"""

from __future__ import annotations

import enum
from collections.abc import Callable

from chronotrace.recorder.events import Event, EventKind
from chronotrace.store import ChronoReader

# Kept as module constants so "how a frame begins and ends" is stated once, and a future
# kind (an await point, a breakpoint event) changes one line rather than four call sites.
ENTRY_KINDS = frozenset({EventKind.CALL})
EXIT_KINDS = frozenset({EventKind.RETURN, EventKind.UNWIND})
"""How a frame begins and ends. Both exits, so `step_out` finds the end of a frame that
blew up -- the case a developer most needs to find."""


class Direction(enum.IntEnum):
    """The sign of the scan, and literally the step increment.

    An `IntEnum` so `seq + direction` just works: direction is the only axis three of the
    four operations vary on, and making it arithmetic means there is no branch on it to
    let forward and backward drift apart.
    """

    FORWARD = 1
    BACKWARD = -1


class Edge(enum.Enum):
    """Why a step could not move -- a value the UI renders, not an error it catches."""

    BEGINNING = "the beginning of the recording"
    END = "the end of the recording"
    LOST_TAIL = "the end of what survived -- this recording was truncated by a crash"


type StepResult = int | Edge
"""A destination `seq`, or the `Edge` that stopped the search.

A union rather than a record with an optional `seq`, so "moved" and "hit a boundary" are
the only two states that can be represented -- there is no `Step(seq=None, edge=None)` to
handle -- and `isinstance(result, Edge)` narrows it under `mypy --strict`.
"""


def seek(
    reader: ChronoReader, seq: int, direction: Direction, match: Callable[[Event], bool]
) -> StepResult:
    """The nearest instant from `seq` in `direction` whose event satisfies `match`.

    This is `continue` and `reverse-continue`, generalised: pass the predicate that
    decides what a breakpoint is. Day 30's retroactive breakpoints are this function with
    an indexed predicate; nothing about the semantics changes, only the cost.

    Args:
        reader: the open recording.
        seq: the instant to start from. Excluded -- a step always moves.
        direction: `Direction.FORWARD` or `Direction.BACKWARD`.
        match: the stop condition, over one event.

    Returns:
        The destination `seq`, or an `Edge` if the scan ran off the recording.

    Raises:
        IndexError: `seq` is not a valid instant. A boundary is a return value; a
            nonexistent starting instant is a caller bug.

    Complexity: O(d) event reads, `d` = the distance travelled. Linear on purpose:
    correct first, indexed later. `d` is 1-27 for `step` on measured recordings but
    unbounded for a predicate that never matches, so a full-recording `seek` costs a full
    scan today.
    """
    # ponytail: linear scan, correct first and indexed later (issue #5). Every operation
    # routes through here, so that upgrade is one function rather than four. The ceiling
    # is real and measured -- a `step_over_back` in a module-level frame scans 281k events
    # and costs 0.63 s. A faster scan is deliberately NOT the fix: iterating decoded blocks
    # would still leave ~126 ms, four times over a frame budget. Only day 30's index, which
    # answers "previous LINE in frame F" as a lookup, actually solves it.
    end = len(reader)
    if not 0 <= seq < end:
        raise IndexError(f"seq {seq} out of range [0, {end})")
    at = seq + direction
    while 0 <= at < end:
        if match(_event(reader, at)):
            return at
        at += direction
    if direction is Direction.BACKWARD:
        return Edge.BEGINNING
    return Edge.LOST_TAIL if reader.truncated else Edge.END


def step(reader: ChronoReader, seq: int, direction: Direction = Direction.FORWARD) -> StepResult:
    """Step into: the nearest stop instant in **any** frame. See the module docstring.

    Complexity: O(d) -- a measured median of 1 event and a maximum of 9, because stop
    instants are dense in a real stream (64% of events are `LINE`). 1.0 us at p50.
    """
    return seek(reader, seq, direction, _is_stop)


def step_over(
    reader: ChronoReader, seq: int, direction: Direction = Direction.FORWARD
) -> StepResult:
    """Step over: the nearest stop instant **in the frame that ran `seq`**.

    Nested calls are skipped because they belong to other frames, so this is a filter on
    `frame_id` -- never a stack walk, and never a filter on `code_id`, which would land in
    a different invocation of the same function under recursion.

    Complexity: O(d), where `d` spans every event of every call stepped over -- a measured
    median of 1 event and a maximum of 280,993 (a call made from a module-level frame).
    Bimodal in time as a result: 1.8 us at p50, 0.63 s at p99. See `seek` on why the fix is
    day 30's index and not a faster scan.
    """
    frame = _event(reader, seq).frame_id
    return seek(reader, seq, direction, lambda e: e.frame_id == frame and _is_stop(e))


def step_out(
    reader: ChronoReader, seq: int, direction: Direction = Direction.FORWARD
) -> StepResult:
    """Leave the current frame: forward to where it exits, backward to where it entered.

    The one operation whose two directions look for different events -- a frame's entry is
    a `CALL` and its exit is a `RETURN`/`UNWIND`. Landing *on* the boundary event rather
    than after it keeps the two symmetric: `reconstruct` at the `CALL` is the frame's first
    instant, and at the exit its last.

    A frame with no `CALL` in the recording (recording began while it was already running
    -- typically the module frame) yields `BEGINNING`, which is the truth: there is no
    recorded instant at which it was entered.

    Complexity: O(d) -- the whole body of the frame, in the worst case.
    """
    frame = _event(reader, seq).frame_id
    wanted = ENTRY_KINDS if direction is Direction.BACKWARD else EXIT_KINDS
    return seek(reader, seq, direction, lambda e: e.frame_id == frame and e.kind in wanted)


def _is_stop(event: Event) -> bool:
    """Whether a debugger would pause here.

    `LINE` only: stepping into a call should land on the callee's first *line*, not on the
    bookkeeping `CALL` before it -- which is what every debugger a developer has used does.
    """
    return event.kind == EventKind.LINE


def _event(reader: ChronoReader, seq: int) -> Event:
    """One event, with the reader's `int | slice` overload narrowed in exactly one place."""
    return reader[seq]  # type: ignore[return-value]
