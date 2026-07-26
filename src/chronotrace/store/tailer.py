"""Read a `.chrono` file that is still being written -- live tailing, as a poll.

Live tailing and crash recovery are the same problem, decided by one fact
------------------------------------------------------------------------
`recovery.py` walks blocks from the header and, at the tail, asks: is this a whole block or a
torn one? A recording killed mid-write has a torn tail, and the classifier discards it. A
recording *still being written* has the exact same torn tail at every instant -- the block the
writer is in the middle of. **Same bytes, different meaning:** in a finished file a bad tail is
corruption; in a live file it is "not finished yet, come back". The only thing that tells them
apart is whether the writer is still alive -- which the file cannot say, so the caller does
(`mark_dead`). This tailer therefore *reuses* `recovery.classify_tail` rather than reimplement
it: one classifier, two callers (crash recovery and this), which is the Day-4 rule -- an
abstraction earned by a second real caller.

Interface: `poll()` -> the events that became complete since the last call, in order, never
repeated; `state` -> RUNNING / COMPLETE / TRUNCATED; `mark_dead()` -> the caller's verdict that
the writer is gone.

What it must never know: HTTP, WebSockets, batching. It reads a growing file and yields
events. It reads values or names for nothing -- those are written only at close (a live tail
sees control flow and density, the detail comes from the finished recording).

Why `read()` from a tracked offset, not mmap
--------------------------------------------
An mmap is fixed at the size it was mapped, so a growing file needs a re-map every poll -- and
on Windows a re-map dance is exactly the kind of platform wart to avoid. A plain buffered read
from the last complete block's offset reads only the new tail each poll, is bounded by the
growth since last poll (not the file size), and never lies about a length the page cache has
not flushed. The reader (random access, whole file) wants mmap; the tailer (sequential,
forward, growing) wants a resume offset. Different access pattern, different tool.

**Windows note, verified on 2026-07-25:** a reader can `open`+`read` a `.chrono` that the
recorder holds open `"wb"`, same process and cross-process -- CPython's `open` already shares
read/write/delete on Windows, so no `FILE_SHARE_*` handling is needed. This was the day's
do-or-die question; it is answered, not deferred.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import TYPE_CHECKING

from chronotrace.store.columnar import events_from_block
from chronotrace.store.constants import (
    EOCD,
    EOCD_MAGIC,
    EOCD_SIZE,
    FORMAT_VERSION_MAJOR,
    HEADER,
    HEADER_SIZE,
    MAGIC,
    BlockType,
    EocdFlag,
)
from chronotrace.store.errors import CorruptRecording, UnsupportedVersion
from chronotrace.store.framing import BlockError, decode_block
from chronotrace.store.recovery import TailStatus, classify_tail

if TYPE_CHECKING:
    import os

    from chronotrace.recorder.events import Event


class TailState(enum.Enum):
    """Where a live recording is: still filling, finished cleanly, or truncated.

    RUNNING is the only state `poll` yields events in. COMPLETE means the footer appeared
    (the recording is now fully readable and scrubbable). TRUNCATED means the footer carried
    the dropped-events flag, or the caller declared the writer dead before a footer arrived --
    either way the prefix is valid and the UI must draw the boundary rather than pretend the
    program ended there.
    """

    RUNNING = "running"
    COMPLETE = "complete"
    TRUNCATED = "truncated"


class ChronoTailer:
    """Polls a growing `.chrono` file and emits newly-complete events, once each, in order.

    Stateless between polls except for one integer: `_pos`, the offset of the next block to
    try. A block that fails to decode (too short, or a CRC that has not settled) is left
    unread -- `_pos` does not advance past it -- so the next poll re-reads it once the writer
    has finished it. That is what makes "retry, don't reject" fall out for free: an incomplete
    tail block is simply a block `_pos` has not reached yet.
    """

    __slots__ = ("_minor", "_path", "_pos", "_started", "_state", "_total", "_truncation")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._pos = 0  # offset of the next block to try; set to HEADER_SIZE once the header lands
        self._minor = 0
        self._started = False
        self._state = TailState.RUNNING
        self._total = 0
        self._truncation: TailStatus | None = None

    @property
    def state(self) -> TailState:
        """RUNNING, COMPLETE, or TRUNCATED -- what `poll` has learned about the recording."""
        return self._state

    @property
    def total(self) -> int:
        """Events emitted so far -- the running length of the live timeline."""
        return self._total

    def poll(self) -> list[Event]:
        """The events that became complete since the last poll, in order, never repeated.

        Empty while waiting for the file to exist, for the header to land, or for the current
        tail block to finish. Once `state` leaves RUNNING (footer seen or writer declared
        dead), always empty.
        """
        if self._state is not TailState.RUNNING:
            return []
        try:
            size = self._path.stat().st_size
        except OSError:
            return []  # file not created yet (or vanished mid-poll): wait
        if not self._started:
            if size < HEADER_SIZE:
                return []  # header still being written
            self._read_header()
        if size <= self._pos:
            return []  # no new complete bytes since last poll
        return self._drain()

    def mark_dead(self) -> None:
        """The caller's verdict: the writer is gone and no footer arrived -- finalize truncated.

        A live recording whose writer died has a torn or clean-but-footerless tail; either way
        no footer means the prefix is all there is. The crash-recovery classifier is run on the
        stuck tail to record *why* it stopped (the same call `walk_blocks` makes of a finished
        file), and the state becomes TRUNCATED. Idempotent, and a no-op once the recording has
        already completed.
        """
        if self._state is not TailState.RUNNING:
            return
        self._truncation = self._classify_stuck_tail()
        self._state = TailState.TRUNCATED

    @property
    def truncation(self) -> TailStatus | None:
        """Why a truncated recording stopped, if `mark_dead` classified it -- else None."""
        return self._truncation

    # -- internals ---------------------------------------------------------------------

    def _read_header(self) -> None:
        """Validate the header and record the format minor; set the walk to the first block."""
        with self._path.open("rb") as handle:
            header = handle.read(HEADER_SIZE)
        magic, major, minor, _flags, _hsize = HEADER.unpack_from(header, 0)
        if magic != MAGIC:
            raise CorruptRecording("not a .chrono file: bad magic")
        if major > FORMAT_VERSION_MAJOR:
            raise UnsupportedVersion(f"file is format v{major}.{minor}; upgrade ChronoTrace")
        self._minor = int(minor)
        self._pos = HEADER_SIZE
        self._started = True

    def _drain(self) -> list[Event]:
        """Decode complete blocks from `_pos` to EOF, collecting EVENTS, spotting the footer."""
        base = self._pos
        with self._path.open("rb") as handle:
            handle.seek(base)
            buf = handle.read()
        events: list[Event] = []
        local = 0
        while local < len(buf):
            try:
                block_type, flags, payload, nxt = decode_block(buf, local)
            except BlockError:
                break  # tail block not finished (short or CRC unsettled): retry next poll
            if block_type == BlockType.INDEX:
                self._state = self._footer_state(buf, nxt)
                break  # the footer: the recording has closed
            if block_type == BlockType.EVENTS:
                events.extend(events_from_block(flags, payload, self._minor))
            local = nxt
        self._pos = base + local
        self._total += len(events)
        return events

    def _footer_state(self, buf: bytes, eocd_offset: int) -> TailState:
        """Read the EOCD that follows the INDEX block: COMPLETE, or TRUNCATED if flagged.

        RUNNING if the EOCD itself is not fully written yet -- the footer is being laid down
        block-first, so an INDEX with no EOCD behind it means "come back".
        """
        if eocd_offset + EOCD_SIZE > len(buf):
            return TailState.RUNNING
        _off, _len, _crc, flags, magic = EOCD.unpack_from(buf, eocd_offset)
        if magic != EOCD_MAGIC:
            return TailState.RUNNING
        return TailState.TRUNCATED if flags & EocdFlag.TRUNCATED else TailState.COMPLETE

    def _classify_stuck_tail(self) -> TailStatus:
        try:
            with self._path.open("rb") as handle:
                handle.seek(self._pos)
                buf = handle.read()
        except OSError:
            return TailStatus.TRUNCATED_PARTIAL
        return classify_tail(buf, 0) if buf else TailStatus.CLEAN
