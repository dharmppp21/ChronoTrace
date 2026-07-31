"""The wire types: everything the browser receives, and nothing the engine stores.

Every type here is built from primitives (`str`, `int`, `float`, `bool`, `None`), enums, and
other DTOs -- deliberately, so `tests/server/test_dto.py` can walk the whole type graph and
prove no storage type (`ProgramState`, `Delta`, `CapturedValue`, `Event`, ...) can reach the
wire. A DTO is the *resolved, display-ready* form of an engine answer: where a `ProgramState`
carries `name_id -> value_ref`, a `Frame` carries `name -> preview string`. That resolution
at the boundary is the whole reason a DTO is not the storage type.

Versioning. The contract is additive-only: new *optional* fields may be added, existing
fields never change type or meaning. `WIRE_VERSION` bumps on any breaking change, which the
additive rule is designed to make rare. The generated OpenAPI spec (day 33, from these types)
is the machine-readable form of this contract; it cannot drift from the code because it is the
code.

Serialisation is `to_wire`, framework-free on purpose: the DTOs are stdlib dataclasses, not
pydantic models, so the wire contract does not depend on FastAPI. FastAPI (day 33) is the
transport; the contract stands without it, and could be served by anything.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass
from typing import Any

WIRE_VERSION = "1"
"""The wire contract version. Additive changes (new optional fields) do not bump it; a change
that removes or repurposes a field does. Kept a string so `1.1` is expressible without a lie
about it being a float."""


# -- sessions ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One row of `GET /api/sessions` -- enough to list a recording, not to open it."""

    id: str
    path: str
    event_count: int
    truncated: bool
    indexed: bool


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """`GET /api/sessions/{id}` -- what the UI needs before it draws anything.

    `truncated` and `indexed` are load-bearing: the timeline must *draw* the truncation
    boundary rather than pretend the recording is whole, and the client decides whether to
    trigger a lazy index build from `indexed`.
    """

    id: str
    path: str
    event_count: int
    truncated: bool
    indexed: bool
    format_version: str
    keyframe_count: int
    python_version: str | None = None
    recorded_at: str | None = None


# -- timeline (scrubber background) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineBucket:
    """One pixel-column of the scrubber: where the bucket starts and how busy it is."""

    first_seq: int
    event_count: int


@dataclass(frozen=True, slots=True)
class Timeline:
    """`GET /api/sessions/{id}/timeline?buckets=N` -- the density background (day-27 index)."""

    buckets: tuple[TimelineBucket, ...]
    total_events: int
    truncated: bool


# -- state (the hot endpoint) -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Variable:
    """One local in a frame -- a *preview*, never the whole value (see `Value` for expansion).

    `ref` is the value's handle: a big list ships its preview and `has_children=True`, and the
    client fetches its rows from `GET /value?ref=` only when the user expands it. That reuses
    day-20 lazy resolution -- the server resolves one ref, not a frame's whole object graph.

    `obj_id` is the value's stable object identity (day-7), so two variables holding the same
    object share an id and the UI can badge the aliasing. It is None for atoms, and -- a known
    limitation (issue #9) -- for `dict`/`list`, which are not weakref-able and so cannot carry a
    reuse-safe id; only a custom object's aliasing is currently badge-able.
    """

    name: str
    preview: str
    ref: int | None
    has_children: bool
    truncated: bool
    obj_id: int | None = None


@dataclass(frozen=True, slots=True)
class ExceptionInfo:
    """An in-flight exception at the instant, resolved to its type name and birth seq."""

    exc_type: str
    raised_at_seq: int
    message: str | None = None


@dataclass(frozen=True, slots=True)
class Frame:
    """One live frame at an instant -- resolved names, not ids. A suspended generator is here."""

    frame_id: int
    function: str
    file: str
    lineno: int
    suspended: bool
    variables: tuple[Variable, ...]


@dataclass(frozen=True, slots=True)
class State:
    """`GET /api/sessions/{id}/state?seq=` -- the instant the scrubber is parked on.

    The hot endpoint: a playhead drag fires it continuously. It is designed to be small and
    cacheable -- previews not values, a strong ETag because a finished recording's state at a
    `seq` never changes (day-11 immutability). `current_frame_id` is the executing frame; the
    rest of `frames` are merely live.
    """

    seq: int
    current_frame_id: int
    frames: tuple[Frame, ...]
    truncated: bool
    exception: ExceptionInfo | None = None


# -- variable diff (day 37) -------------------------------------------------------------


class ChangeKind(enum.Enum):
    """How a binding changed between two instants -- the three colours a diff panel paints."""

    ADDED = "added"  # the name did not exist at seq-1 (a creation)
    REMOVED = "removed"  # the name is gone at seq (a `del`)
    MODIFIED = "modified"  # the name's value changed, old -> new


@dataclass(frozen=True, slots=True)
class VarChange:
    """One binding that changed at an instant -- a preview pair, the diff panel's row."""

    frame_id: int
    name: str
    kind: ChangeKind
    old: str | None = None  # the previous value's preview (REMOVED / MODIFIED)
    new: str | None = None  # the new value's preview (ADDED / MODIFIED)


@dataclass(frozen=True, slots=True)
class Diff:
    """`GET /api/sessions/{id}/diff?seq=` -- exactly what changed from `seq-1` to `seq`.

    A *lookup*, not a comparison of two reconstructed states: the day-16 invertible deltas
    already carry each binding's old and new ref, so the diff is the BIND deltas at `seq` with
    both refs resolved to previews. Storing the old ref 21 days ago is why this is O(changes),
    not O(locals) over two full states -- the whole point of paying those bytes.

    What it shows, and what it cannot: a *content* change (the same object mutated, or rebound
    to different content) is MODIFIED with `old != new`. An *identity-only* change (rebound to a
    different object of equal content) is coalesced by day-8 content-addressed dedup -- the two
    refs are equal, so no delta exists and it is not shown. And REMOVED (`del x`) is rare,
    because the recorder does not observe a name leaving `f_locals` (a tracked limitation).
    """

    seq: int
    changes: tuple[VarChange, ...]


# -- lazy value expansion ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueChild:
    """One element/attribute of an expanded value -- itself a preview, expandable in turn."""

    key: str
    preview: str
    ref: int | None
    has_children: bool


@dataclass(frozen=True, slots=True)
class Value:
    """`GET /api/sessions/{id}/value?ref=` -- one level of a value, expanded on click.

    One level only: a deep or wide structure is walked lazily, a click at a time, so a 10k-
    element list never ships 10k elements to render one row. `truncated` says the capture
    itself only saw a prefix -- distinct from "not yet expanded" (`children is None`).
    """

    preview: str
    kind: str
    truncated: bool
    children: tuple[ValueChild, ...] | None = None


# -- source + heatmap -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineHeat:
    """How many times one source line executed -- the heatmap gutter (day-27 line index)."""

    lineno: int
    count: int


@dataclass(frozen=True, slots=True)
class Source:
    """`GET /api/sessions/{id}/source?file=` -- source text with a per-line execution heatmap.

    `lines` is the file on disk *now* (empty only when it is gone). `available` says whether the
    heatmap's line numbers align to those lines: True when the recording's source hash (format
    1.7) still matches, False when the file changed since recording or was never hashed. On
    False the UI shows the current source but withholds the heatmap overlay -- a wrong line is
    worse than no line.
    """

    file: str
    lines: tuple[str, ...]
    heatmap: tuple[LineHeat, ...]
    available: bool


# -- call tree --------------------------------------------------------------------------


class ExitKind(enum.Enum):
    """How a frame ended -- the single most useful thing a call tree colours.

    `raised` is a frame CPython unwound because of an exception (day-6's UNWIND), painted
    distinctly so the tree answers "where did it blow up?" at a glance. `open` is a frame that
    never returned in the recording -- still live at the end, a suspended generator, or a
    crash-truncated tail (`exit_seq IS NULL`); not an error, just not-yet-returned.
    """

    RETURNED = "returned"
    RAISED = "raised"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class CallFrame:
    """One node of the call tree -- function, its call and return instants, its parent, its fate.

    `exit_seq` is where the frame left (RETURN/UNWIND): the jump-to-return target, and with
    `entry_seq` the event span of the call. `None` when the frame never returned (`exit_kind`
    is then `open`). Even a frame still live at the current instant carries its *eventual* exit
    -- the recording is complete, so the tree can show a live frame's future fate.
    """

    frame_id: int
    function: str
    file: str
    entry_seq: int
    exit_seq: int | None
    exit_kind: ExitKind
    parent_frame_id: int | None


@dataclass(frozen=True, slots=True)
class CallTree:
    """`GET /api/sessions/{id}/calltree` -- frames live at `seq`, or one frame's direct children.

    `next_cursor` is set only in children mode (`?parent=&after=`): a busy frame can have
    millions of direct calls, so children page exactly as a query does; `None` means stack mode
    or the last page. Stack mode is bounded by call depth and never pages.
    """

    frames: tuple[CallFrame, ...]
    next_cursor: int | None = None


# -- query ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """`POST /api/sessions/{id}/query` body -- a typed query by name, with primitive args.

    Args are a flat map of primitives (`{"name": "total", "before_seq": 500}`), which is all
    any shipped query needs; a query is dispatched by `name` through the day-28 registry. Not a
    string DSL -- the API is the typed surface, exactly as the CLI is (the day-28 #13 decision).
    """

    name: str
    args: dict[str, str | int | bool | None]
    cursor: int | None = None
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class QueryHit:
    """One result of a query -- a jumpable instant with just enough display context."""

    seq: int
    file: str | None = None
    lineno: int | None = None
    function: str | None = None
    value_preview: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class QueryResult:
    """`POST /query` response -- one page of instants, a cursor, and whether it is partial."""

    hits: tuple[QueryHit, ...]
    next_cursor: int | None
    partial: bool


@dataclass(frozen=True, slots=True)
class ArgSpec:
    """One argument of a query, so a client builds its form field without hardcoding it.

    `type` is the wire type name (`"string"`, `"integer"`) the UI maps to an input; `required`
    is whether the query's constructor demands it (has no default). Derived by introspecting the
    query's dataclass fields, so it cannot drift from what the query actually accepts.
    """

    name: str
    type: str
    required: bool


@dataclass(frozen=True, slots=True)
class QueryDescriptor:
    """`GET /api/queries` -- one registered query, so the UI builds its form from the API itself.

    The day-28 registry paying off: add a query on the backend and it appears in the query panel
    with its inputs, no frontend change. The API describes its own queries.
    """

    name: str
    summary: str
    args: tuple[ArgSpec, ...]


# -- stepping ---------------------------------------------------------------------------


class StepEdge(enum.Enum):
    """Why a step did not move -- a value the UI renders (a disabled button), not an error."""

    BEGINNING = "beginning"
    END = "end"
    LOST_TAIL = "lost_tail"


@dataclass(frozen=True, slots=True)
class StepResult:
    """`GET /api/sessions/{id}/step?seq=&dir=&mode=` -- the destination instant, or an edge.

    Exactly one of `seq`/`edge` is set: a move returns a `seq`, running off either end returns
    the `edge` the UI shows as a boundary (a truncated tail's `LOST_TAIL` is not the program's
    end, and the UI must not conflate them).
    """

    seq: int | None
    edge: StepEdge | None


# -- live stream (day 34) ---------------------------------------------------------------


class StreamState(enum.Enum):
    """Where a live recording is, on the wire -- the resolved form of `store.TailState`.

    A wire enum of its own rather than the storage one, for the same reason every DTO is not
    its storage type: `server` must be free to serve a state the store does not name, and the
    store free to gain a state the wire does not yet expose.
    """

    RUNNING = "running"
    COMPLETE = "complete"  # the footer arrived: the recording is now fully scrubbable
    TRUNCATED = "truncated"  # dropped events, or the writer died before a footer


@dataclass(frozen=True, slots=True)
class DensityDelta:
    """New events in one timeline bucket since the last frame -- the scrubber fills from these.

    Density, not events: a live view needs the *shape* of activity (where it is dense), not
    every event. The detail is a `/state` call away once the user parks the playhead, exactly
    as the day-27 density index trades the whole event stream for a per-bucket count.
    """

    first_seq: int
    event_count: int


@dataclass(frozen=True, slots=True)
class NotableEvent:
    """A notable instant worth a marker as it happens -- an exception being born, live."""

    seq: int
    kind: str
    lineno: int


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """One WebSocket message: the aggregate of a batch of events, never the raw events.

    Aggregated on purpose. At 100k events/sec, one message per event drowns the browser; one
    message per poll carrying a bounded density delta plus a capped list of notable events does
    not, and it is why server memory stays flat under a slow client -- the file is the source of
    truth, so anything that does not fit in a frame is summarised (`dropped`), never queued.
    """

    total_events: int
    state: StreamState
    density: tuple[DensityDelta, ...]
    notable: tuple[NotableEvent, ...]
    dropped: int


# -- errors (RFC 7807-ish) --------------------------------------------------------------


class ErrorCode(enum.Enum):
    """The day-13 error hierarchy on the wire. Each maps to a status *and a user action*."""

    CORRUPT = "corrupt"  # 422: the recording is damaged; re-record
    TRUNCATED_SEQ = "truncated_seq"  # 404: seq is in the lost tail of a crashed recording
    UNSUPPORTED_VERSION = "unsupported_version"  # 422: upgrade ChronoTrace
    NOT_INDEXED = "not_indexed"  # 409: build the index (or retry after the lazy build)
    SEQ_OUT_OF_RANGE = "seq_out_of_range"  # 404: pick a seq in [0, event_count)
    NOT_FOUND = "not_found"  # 404: no such session/file
    UNKNOWN_QUERY = "unknown_query"  # 400: no query registered under that name
    BAD_REQUEST = "bad_request"  # 400: the request is malformed -- bad params or query args


@dataclass(frozen=True, slots=True)
class Problem:
    """A problem-details error body -- a distinct code and message per distinct user action.

    Modelled on RFC 7807. `status` is the HTTP status; `code` is the machine-readable reason a
    client branches on; `detail` is the human sentence. The whole reason to distinguish, say,
    a corrupt recording from a seq out of range is that the *user does something different*
    about each -- re-record vs pick another instant.
    """

    code: ErrorCode
    status: int
    title: str
    detail: str
    instance: str | None = None


STATUS_FOR: dict[ErrorCode, int] = {
    ErrorCode.CORRUPT: 422,  # the file is damaged -- re-record
    ErrorCode.TRUNCATED_SEQ: 404,  # the instant was in the crash-lost tail
    ErrorCode.UNSUPPORTED_VERSION: 422,  # a newer format -- upgrade
    ErrorCode.NOT_INDEXED: 409,  # index missing -- build it, or retry after the lazy build
    ErrorCode.SEQ_OUT_OF_RANGE: 404,  # pick a seq in [0, event_count)
    ErrorCode.NOT_FOUND: 404,  # no such session or file
    ErrorCode.UNKNOWN_QUERY: 400,  # no query registered under that name
    ErrorCode.BAD_REQUEST: 400,  # the request itself is malformed
}
"""Canonical code -> HTTP status. The mapping is the contract, so it lives with the DTOs and is
tested exhaustive; the day-33 server reads it rather than hard-coding a status per raise site."""


def problem(code: ErrorCode, detail: str, instance: str | None = None) -> Problem:
    """Build a `Problem` with the canonical status for `code`. The one place errors are made."""
    return Problem(
        code=code,
        status=STATUS_FOR[code],
        title=code.value.replace("_", " "),
        detail=detail,
        instance=instance,
    )


def to_wire(obj: Any) -> Any:
    """Serialise a DTO (or a container of DTOs) to JSON-native data. The framework-free path.

    Dataclasses become dicts, tuples become lists, enums become their `.value`; everything
    else is already primitive. This is the exact bytes shape the golden tests lock and FastAPI
    would otherwise produce -- keeping it here means the wire shape is testable without a server
    and the contract owes nothing to the transport.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_wire(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_wire(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_wire(v) for k, v in obj.items()}
    return obj
