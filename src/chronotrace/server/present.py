"""Turn an engine answer into a wire DTO. The whole "no storage type reaches the wire" rule.

Problem this solves: a route must not build DTOs inline, because two routes render the same
thing (a captured value as a preview) and a copy in each is the duplicated logic the project
forbids -- and because the day-32 contract's one law is that a response is a *resolved* DTO,
never a `ProgramState`/`CapturedValue`/`Delta`. So the resolution -- ids to names, refs to
previews, engine records to wire records -- happens here, in one auditable place, and
`tests/server/test_dto.py` proves the output graph contains no storage type.

Interface: one function per response DTO (`state`, `value`, `timeline`, `source`,
`call_tree`, `step_result`, `query_result`, `session_summary`, `session_meta`).

It must never know: HTTP, caching, the request. It takes engine data and returns a DTO.
Pure functions, so a route is `parse -> call a layer -> present.x -> return`.

Value expansion is one level deep, on purpose
----------------------------------------------
A captured value is stored inlined -- one pool blob is a whole bounded tree, with no
per-child pool ref (`store.reader.value`). So `value()` returns a value's *immediate*
children (`ValueChild.ref` is None for them, since they have no pool address of their own).
That matches the frozen `ValueChild` shape, which carries no recursion. Drilling deeper than
one level would need path-addressable refs, a contract change -- tracked as issue #15. The
laziness that matters is per *variable*: `/state` ships previews and resolves a variable's
tree only when `/value` is called for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from chronotrace.query._resolve import MAX_PREVIEW_CHARS, render_captured
from chronotrace.reconstruct import Edge
from chronotrace.server import dto

if TYPE_CHECKING:
    from chronotrace.query import Hit, QueryContext
    from chronotrace.query import QueryResult as EngineQueryResult
    from chronotrace.reconstruct import ExceptionState, FrameState, ProgramState
    from chronotrace.reconstruct import StepResult as EngineStep
    from chronotrace.store import ChronoReader, Strings

_SEQ_TAGS = frozenset({"list", "tuple", "set", "frozenset"})
_CONTAINER_TAGS = _SEQ_TAGS | {"dict"}

_EDGE: dict[Edge, dto.StepEdge] = {
    Edge.BEGINNING: dto.StepEdge.BEGINNING,
    Edge.END: dto.StepEdge.END,
    Edge.LOST_TAIL: dto.StepEdge.LOST_TAIL,
}


# -- sessions ---------------------------------------------------------------------------


def session_summary(sid: str, path: str, reader: ChronoReader, indexed: bool) -> dto.SessionSummary:
    """One listing row -- enough to show a recording without opening its index."""
    return dto.SessionSummary(
        id=sid,
        path=path,
        event_count=len(reader),
        truncated=reader.truncated,
        indexed=indexed,
    )


def session_meta(sid: str, path: str, reader: ChronoReader, indexed: bool) -> dto.SessionMeta:
    """Everything the UI needs before it draws.

    `python_version`/`recorded_at` stay None -- the format does not record them, and inventing
    a value would be a lie about the run.
    """
    return dto.SessionMeta(
        id=sid,
        path=path,
        event_count=len(reader),
        truncated=reader.truncated,
        indexed=indexed,
        format_version=reader.format_version,
        keyframe_count=reader.keyframe_count(),
    )


# -- timeline ---------------------------------------------------------------------------


def timeline(
    rows: list[tuple[int, int, int]], total: int, truncated: bool, buckets: int | None
) -> dto.Timeline:
    """The density profile as scrubber columns, downsampled to `buckets` if fewer are asked."""
    if buckets is not None and 0 < buckets < len(rows):
        rows = _downsample(rows, buckets)
    return dto.Timeline(
        buckets=tuple(
            dto.TimelineBucket(first_seq=first, event_count=count) for _b, first, count in rows
        ),
        total_events=total,
        truncated=truncated,
    )


def _downsample(rows: list[tuple[int, int, int]], n: int) -> list[tuple[int, int, int]]:
    """Merge `len(rows)` buckets into `n`, summing counts and keeping each group's first seq."""
    size = -(-len(rows) // n)  # ceil, so the result never exceeds n groups
    out: list[tuple[int, int, int]] = []
    for i in range(0, len(rows), size):
        group = rows[i : i + size]
        out.append((group[0][0], group[0][1], sum(r[2] for r in group)))
    return out


# -- state (the hot endpoint) -----------------------------------------------------------


def state(ctx: QueryContext, ps: ProgramState) -> dto.State:
    """A `ProgramState` (ids and refs) resolved to a `State` (names and previews)."""
    strings = ctx.reader.strings()
    return dto.State(
        seq=ps.seq,
        current_frame_id=ps.current_frame_id,
        frames=tuple(_frame(ctx, strings, f) for f in ps.frames),
        truncated=ctx.reader.truncated,
        exception=None if ps.exception is None else _exception(strings, ps.exception),
    )


def _frame(ctx: QueryContext, strings: Strings, f: FrameState) -> dto.Frame:
    variables = tuple(
        _variable(ctx, _name(strings, name_id), value_ref)
        for name_id, value_ref in sorted(f.bindings.items(), key=lambda kv: _name(strings, kv[0]))
    )
    return dto.Frame(
        frame_id=f.frame_id,
        function=_qualname(strings, f.code_id),
        file=_filename(strings, f.code_id),
        lineno=f.lineno,
        suspended=f.suspended,
        variables=variables,
    )


def _variable(ctx: QueryContext, name: str, value_ref: int) -> dto.Variable:
    """One local as a preview.

    A ref the pool has lost renders as unavailable, never as a fake value -- the resolve
    layer's rule, honoured at the boundary.
    """
    from chronotrace.reconstruct import MissingValue

    try:
        captured = ctx.resolver.resolve(value_ref)
    except MissingValue:
        return dto.Variable(
            name=name, preview="<unavailable>", ref=None, has_children=False, truncated=False
        )
    preview, _kind, has_children, truncated = _inspect(captured)
    return dto.Variable(
        name=name, preview=preview, ref=value_ref, has_children=has_children, truncated=truncated
    )


def _exception(strings: Strings, exc: ExceptionState) -> dto.ExceptionInfo:
    return dto.ExceptionInfo(
        exc_type=_exc_type(strings, exc.exc_type_id), raised_at_seq=exc.raised_at_seq
    )


# -- value expansion --------------------------------------------------------------------


def value(captured: Any) -> dto.Value:
    """One captured value as its immediate children -- see the module docstring on depth."""
    preview, kind, _has, truncated = _inspect(captured)
    return dto.Value(preview=preview, kind=kind, truncated=truncated, children=_children(captured))


def _children(captured: Any) -> tuple[dto.ValueChild, ...] | None:
    """The immediate level of a container/object, or None for a leaf (nothing to expand)."""
    if not _tagged(captured):
        return None
    tag = captured["$"]
    if tag == "dict":
        return tuple(_child(render_captured(k), v) for k, v in captured.get("items", []))
    if tag in _SEQ_TAGS:
        return tuple(_child(str(i), v) for i, v in enumerate(captured.get("items", [])))
    if tag == "obj" and captured.get("attrs"):
        return tuple(_child(str(k), v) for k, v in captured["attrs"].items())
    return None


def _child(key: str, v: Any) -> dto.ValueChild:
    return dto.ValueChild(
        key=key, preview=_clip(render_captured(v)), ref=None, has_children=_has_children(v)
    )


# -- source + call tree -----------------------------------------------------------------


def source(path: str, lines: list[str], heat: dict[int, int], available: bool) -> dto.Source:
    """Source text with its per-line heatmap.

    `available` is False when the on-disk file no longer matches the recording's hash -- the UI
    then draws the heatmap over a placeholder, never over lines that may be a different program.
    """
    return dto.Source(
        file=path,
        lines=tuple(lines),
        heatmap=tuple(dto.LineHeat(lineno=ln, count=c) for ln, c in sorted(heat.items())),
        available=available,
    )


def call_tree(rows: list[tuple[int, int, int, int | None]], strings: Strings) -> dto.CallTree:
    """The frames live at an instant, each with its parent so the UI can nest them."""
    return dto.CallTree(
        frames=tuple(
            dto.CallFrame(
                frame_id=fid,
                function=_qualname(strings, code_id),
                file=_filename(strings, code_id),
                entry_seq=entry,
                parent_frame_id=parent,
            )
            for fid, code_id, entry, parent in rows
        )
    )


# -- stepping + query -------------------------------------------------------------------


def step_result(result: EngineStep) -> dto.StepResult:
    """A destination `seq`, or the boundary `Edge` the UI renders as a disabled button."""
    if isinstance(result, Edge):
        return dto.StepResult(seq=None, edge=_EDGE[result])
    return dto.StepResult(seq=result, edge=None)


def query_result(qr: EngineQueryResult) -> dto.QueryResult:
    """A page of query hits, its `seq` cursor flattened, and whether the answer is partial."""
    return dto.QueryResult(
        hits=tuple(_hit(h) for h in qr.hits),
        next_cursor=None if qr.next_cursor is None else qr.next_cursor.after_seq,
        partial=qr.partial,
    )


def _hit(h: Hit) -> dto.QueryHit:
    return dto.QueryHit(
        seq=h.seq,
        file=h.file,
        lineno=h.lineno,
        function=h.function,
        value_preview=h.value_preview,
        note=h.note,
    )


# -- captured-value inspection ----------------------------------------------------------


def _inspect(captured: Any) -> tuple[str, str, bool, bool]:
    """`(preview, kind, has_children, truncated)` for a captured value.

    Reuses the query layer's `render_captured`, so a value looks identical in a query result
    and here.
    """
    preview = _clip(render_captured(captured))
    if not _tagged(captured):
        return preview, type(captured).__name__, False, False
    tag = captured["$"]
    kind = str(captured.get("type", tag)) if tag == "obj" else str(tag)
    return preview, kind, _has_children(captured), bool(captured.get("truncated"))


def _has_children(captured: Any) -> bool:
    if not _tagged(captured):
        return False
    tag = captured["$"]
    if tag in _CONTAINER_TAGS:
        return bool(captured.get("items"))
    return tag == "obj" and bool(captured.get("attrs"))


def _tagged(captured: Any) -> bool:
    """Whether this is a captured container, not an atom.

    A dict carrying the `"$"` tag that `capture` stamps on every non-atom. A user dict is
    itself captured as `{"$": "dict", ...}`, so user data cannot spoof the marker.
    """
    return isinstance(captured, dict) and "$" in captured


def _clip(text: str) -> str:
    return text if len(text) <= MAX_PREVIEW_CHARS else f"{text[:MAX_PREVIEW_CHARS]}..."


# -- id -> text, all guarded so a lost table degrades rather than crashes ----------------


def _name(strings: Strings, name_id: int) -> str:
    return strings.names[name_id] if 0 <= name_id < len(strings.names) else f"name#{name_id}"


def _qualname(strings: Strings, code_id: int) -> str:
    if 0 <= code_id < len(strings.codes):
        return strings.codes[code_id].qualname
    return f"code#{code_id}"


def _filename(strings: Strings, code_id: int) -> str:
    return strings.codes[code_id].filename if 0 <= code_id < len(strings.codes) else ""


def _exc_type(strings: Strings, exc_type_id: int) -> str:
    if 0 <= exc_type_id < len(strings.exc_types):
        return strings.exc_types[exc_type_id]
    return f"exc#{exc_type_id}"
