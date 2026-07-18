"""The append-only `.chrono` writer, and `FileSink` -- the recorder's road to disk.

`ChronoWriter` turns an event stream into the exact bytes `docs/format-spec.md`
describes: a header, an (empty, for now) META block, columnar EVENTS blocks framed
by `framing.py`, then an INDEX and EOCD written last. `FileSink` wraps it as the
second implementation of the Day 4 `Sink` protocol -- the moment that protocol
earns its keep, because the recorder emits into it knowing nothing about files.

Durability (ADR-0004, restated as a contract)
---------------------------------------------
There is **no per-block `fsync`**. This is a debug artifact; fsyncing every block
would gate the traced program on disk latency and could halve its speed, to protect
data nobody will miss after a crash. Durability comes from *framing*, not fsync:
any block that reached disk is self-validating, so the readable prefix is intact
regardless. `FileSink` fsyncs once, at clean close.

Backpressure: never break the program, accept slowing it
--------------------------------------------------------
Day 5's rule -- the user's program correctness outranks our recording -- decides
this. A **failed** write (disk full, I/O error) never propagates to the program:
the sink drops the rest of the stream and marks the recording `truncated` so the UI
can say so. A **slow** disk *blocks* the recorder during a block flush; the program
runs correct, just slower, which is not a correctness violation and not worth the
complexity of a background-writer drop-queue until it is measured to matter
(tracked, not hidden).

Compression and the value pool (day 14)
---------------------------------------
EVENTS blocks are zstd-compressed as they flush (`compression.py`), on the traced
program's own thread -- which is why the level is the measured speed/ratio knee, not
the smallest. The content-addressed VALUES section is written once, at close, from the
whole pool (`add_value` collects it; `valuepool.py` owns the write-once + collision
logic). META and INDEX stay uncompressed: both are tiny and read at open, so a
decompress step there would only slow the thing compression exists to keep fast.

Keyframes (day 15)
------------------
The **writer** owns keyframe cadence -- one every `keyframe_interval` events -- because
cadence is a storage strategy and the recorder must not know storage exists (the
dependency arrow). The writer already sees every event, so it folds each into a
`LiveState` projection and, on the interval, snapshots that state as a KEYFRAMES block.
A keyframe stores live frames' locals as `ValueRef`s only (the pool holds the values),
so it is cheap. `seq` 0 always gets one, giving reconstruction a floor it can never
fall off the start of.

Why META is still empty
-----------------------
The META block will carry the config snapshot -- `max_depth`, scope, redaction --
because a recording made with `max_depth=3` is not interpretable six months later
without it. That is a msgpack map (the codec now exists), but wiring the resolved
config down to the writer is a later day's plumbing; until then META is the
spec-permitted empty map, and the reservation stays deliberate.
"""

from __future__ import annotations

import os
import zlib
from typing import BinaryIO

from chronotrace.recorder.capture import CapturedValue
from chronotrace.recorder.events import Event
from chronotrace.recorder.values import ValueRef
from chronotrace.store.columnar import COUNT_SIZE, encode_events
from chronotrace.store.compression import compress
from chronotrace.store.constants import (
    EOCD,
    EOCD_MAGIC,
    FORMAT_VERSION_MAJOR,
    FORMAT_VERSION_MINOR,
    HEADER,
    HEADER_SIZE,
    INDEX_ENTRY,
    MAGIC,
    BlockFlag,
    BlockType,
    EocdFlag,
)
from chronotrace.store.delta import DELTA_RANGE, Delta, derive, encode_deltas
from chronotrace.store.framing import encode_block
from chronotrace.store.keyframe import DEFAULT_KEYFRAME_INTERVAL, KF_SEQ, LiveState
from chronotrace.store.valuepool import ValuePoolWriter

DEFAULT_BLOCK_EVENTS = 65536
"""Events per EVENTS block. Bigger compresses better and shrinks the index; smaller
makes a point read decode less. 65536 is a starting point, not a tuned value --
day 18's file-size experiment validates or moves it (ADR-0004)."""

_EMPTY_META = b"\x80"  # msgpack empty map; config META lands day 14 (see module docstring)


class ChronoWriter:
    """Writes the `.chrono` byte stream. Format logic only -- no file, no fsync.

    Operates on any writable binary stream, so tests drive it with `io.BytesIO`
    and never touch a disk. Buffers events into blocks of `block_events`, frames
    each, and records its location; `close` appends the INDEX and EOCD. An index
    entry is recorded only *after* a block's bytes are written, so a write that
    fails mid-block leaves that block out of the index -- the readable prefix never
    references a torn block.
    """

    __slots__ = (
        "_block_events",
        "_buffer",
        "_closed",
        "_deltas",
        "_index",
        "_interval",
        "_live",
        "_offset",
        "_pool",
        "_stream",
    )

    def __init__(
        self,
        stream: BinaryIO,
        *,
        block_events: int = DEFAULT_BLOCK_EVENTS,
        keyframe_interval: int = DEFAULT_KEYFRAME_INTERVAL,
    ) -> None:
        self._stream = stream
        self._block_events = block_events
        self._interval = keyframe_interval
        self._buffer: list[Event] = []
        self._deltas: list[Delta] = []
        self._live = LiveState()
        self._pool = ValuePoolWriter()
        self._index: list[tuple[int, int, int]] = []  # (block_type, offset, length)
        self._offset = 0
        self._closed = False
        self._write(HEADER.pack(MAGIC, FORMAT_VERSION_MAJOR, FORMAT_VERSION_MINOR, 0, HEADER_SIZE))
        self._write_block(BlockType.META, _EMPTY_META)

    def add(self, event: Event) -> None:
        """Buffer one event, snapshot on the keyframe cadence, flush a full block.

        The keyframe is taken *after* folding the event into the live state, so it is
        the state after `seq` -- and `seq` 0 lands on the interval, so a recording
        always has a keyframe floor. May raise on a write failure.
        """
        self._buffer.append(event)
        # Derive deltas from the live state BEFORE folding the event in -- a bind's old
        # ref is the current binding, which the event is about to overwrite.
        self._deltas.extend(derive(event, self._live.frames))
        self._live.apply(event)
        if event.seq % self._interval == 0:
            self._emit_keyframe(event.seq)
        if len(self._buffer) >= self._block_events:
            self._flush()

    def add_value(self, captured: CapturedValue) -> ValueRef:
        """Intern one captured value into the pool; return the `ValueRef` events cite.

        Content-addressed and write-once (see `valuepool.py`): identical content
        returns the same reference. The pool is buffered and written as the VALUES
        section at `close`, so the whole recording's values share one directory.
        """
        return self._pool.add(captured)

    def close(self, *, truncated: bool = False) -> None:
        """Flush events, write the value pool, then the index and EOCD. Idempotent.

        Args:
            truncated: mark the recording incomplete (events were dropped).
        """
        if self._closed:
            return
        self._closed = True
        self._flush()
        if self._pool:
            # ponytail: one VALUES block. Split into size-bounded blocks with a global
            # ref->block directory only when a pool outgrows a block (day 15+ needs it).
            self._write_block(
                BlockType.VALUES, compress(self._pool.encode()), BlockFlag.COMPRESSED_ZSTD
            )
        index_payload = b"".join(INDEX_ENTRY.pack(t, o, ln) for t, o, ln in self._index)
        index_offset = self._offset
        self._write_block(BlockType.INDEX, index_payload)
        flags = EocdFlag.TRUNCATED if truncated else EocdFlag.NONE
        self._write(
            EOCD.pack(
                index_offset,
                self._offset - index_offset,
                zlib.crc32(index_payload),
                flags,
                EOCD_MAGIC,
            )
        )
        self._stream.flush()

    def _emit_keyframe(self, seq: int) -> None:
        # The u64 seq stays uncompressed at the front so the reader can peek which
        # instant a keyframe snapshots without decompressing it (the seq index).
        payload = KF_SEQ.pack(seq) + compress(self._live.encode())
        self._write_block(BlockType.KEYFRAMES, payload, BlockFlag.COMPRESSED_ZSTD)

    def _flush(self) -> None:
        if not self._buffer:
            return
        # Keep the u32 event count uncompressed at the front so the reader can index by
        # seq without decompressing every block (peek_count); compress only the columns.
        # The CRC (in encode_block) then covers the stored bytes, as the spec requires.
        blob = encode_events(self._buffer)
        payload = blob[:COUNT_SIZE] + compress(blob[COUNT_SIZE:])
        self._write_block(BlockType.EVENTS, payload, BlockFlag.COMPRESSED_ZSTD)
        self._buffer.clear()
        self._flush_deltas()

    def _flush_deltas(self) -> None:
        if not self._deltas:
            return
        # The (first, last) seq span stays uncompressed so the reader answers
        # deltas_between() by peeking a block's range without decompressing it.
        span = DELTA_RANGE.pack(self._deltas[0].seq, self._deltas[-1].seq)
        payload = span + compress(encode_deltas(self._deltas))
        self._write_block(BlockType.DELTAS, payload, BlockFlag.COMPRESSED_ZSTD)
        self._deltas.clear()

    def _write_block(
        self, block_type: BlockType, payload: bytes, flags: BlockFlag = BlockFlag.NONE
    ) -> None:
        block = encode_block(block_type, payload, flags)
        offset = self._offset
        self._write(block)  # may raise; index entry is recorded only after success
        # Flush the completed block to the OS (not fsync). This is the durability half
        # of the crash guarantee: a block in the OS page cache survives a kill -9 (the
        # kernel owns the cache), so recovery.walk_blocks can recover it. fsync is still
        # deliberately skipped -- see ADR-0004 and store/recovery.py.
        self._stream.flush()
        self._index.append((block_type, offset, len(block)))

    def _write(self, data: bytes) -> None:
        self._stream.write(data)
        self._offset += len(data)


class FileSink:
    """A `Sink` that writes a `.chrono` file, dropping on failure, never blocking.

    See the module docstring for the durability and backpressure contracts.
    """

    __slots__ = ("_closed", "_dropping", "_file", "_writer")

    def __init__(
        self, path: str | os.PathLike[str], *, block_events: int = DEFAULT_BLOCK_EVENTS
    ) -> None:
        # "wb", never "w": Windows text mode would translate every 0x0A byte in a
        # block to 0x0D 0x0A and corrupt the binary format (the day-1 CRLF trap).
        self._file = open(path, "wb")  # noqa: SIM115, PTH123 -- lifetime spans emit()/close()
        self._writer = ChronoWriter(self._file, block_events=block_events)
        self._dropping = False
        self._closed = False

    def emit(self, event: Event) -> None:
        """Accept one event; drop the rest of the stream if the disk fails. Never raises."""
        if self._dropping:
            return
        try:
            self._writer.add(event)
        except OSError:
            self._dropping = True  # disk failed: keep the writer for the footer, drop events

    def close(self) -> None:
        """Write the footer, fsync once, close. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close(truncated=self._dropping)
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError:
            pass  # a failed footer/fsync leaves a footerless file, recovered by scan
        finally:
            self._file.close()

    @property
    def truncated(self) -> bool:
        """True if events were dropped -- the recording is a prefix, not the whole."""
        return self._dropping
