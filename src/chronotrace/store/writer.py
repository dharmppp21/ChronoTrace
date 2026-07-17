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

Why META is empty today
-----------------------
The META block will carry the config snapshot -- `max_depth`, scope, redaction --
because a recording made with `max_depth=3` is not interpretable six months later
without it, and nobody remembers. That content is msgpack, which lands on day 14;
until then META is the spec-permitted empty map, and the rationale is written down
here so the reservation is deliberate.
"""

from __future__ import annotations

import os
import zlib
from typing import BinaryIO

from chronotrace.recorder.events import Event
from chronotrace.store.columnar import encode_events
from chronotrace.store.constants import (
    EOCD,
    EOCD_MAGIC,
    FORMAT_VERSION_MAJOR,
    FORMAT_VERSION_MINOR,
    HEADER,
    HEADER_SIZE,
    INDEX_ENTRY,
    MAGIC,
    BlockType,
    EocdFlag,
)
from chronotrace.store.framing import encode_block

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

    __slots__ = ("_block_events", "_buffer", "_closed", "_index", "_offset", "_stream")

    def __init__(self, stream: BinaryIO, *, block_events: int = DEFAULT_BLOCK_EVENTS) -> None:
        self._stream = stream
        self._block_events = block_events
        self._buffer: list[Event] = []
        self._index: list[tuple[int, int, int]] = []  # (block_type, offset, length)
        self._offset = 0
        self._closed = False
        self._write(HEADER.pack(MAGIC, FORMAT_VERSION_MAJOR, FORMAT_VERSION_MINOR, 0, HEADER_SIZE))
        self._write_block(BlockType.META, _EMPTY_META)

    def add(self, event: Event) -> None:
        """Buffer one event, flushing a full block. May raise on a write failure."""
        self._buffer.append(event)
        if len(self._buffer) >= self._block_events:
            self._flush()

    def close(self, *, truncated: bool = False) -> None:
        """Flush the last block, write the index and EOCD. Idempotent.

        Args:
            truncated: mark the recording incomplete (events were dropped).
        """
        if self._closed:
            return
        self._closed = True
        self._flush()
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

    def _flush(self) -> None:
        if not self._buffer:
            return
        self._write_block(BlockType.EVENTS, encode_events(self._buffer))
        self._buffer.clear()

    def _write_block(self, block_type: BlockType, payload: bytes) -> None:
        block = encode_block(block_type, payload)
        offset = self._offset
        self._write(block)  # may raise; index entry is recorded only after success
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
