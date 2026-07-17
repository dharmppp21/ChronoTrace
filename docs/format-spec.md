# The `.chrono` file format — normative specification

**Format version 1.1.** This document defines the on-disk `.chrono` format
precisely enough to implement a reader or writer in any language. Where it and the
code disagree, this document is the contract; `src/chronotrace/store/constants.py`
is its machine form and `tests/store/test_constants.py` pins the byte layout.

Version 1.1 (day 14) activated per-block zstd compression (the `COMPRESSED_ZSTD`
flag reserved in 1.0) and made the VALUES section real msgpack. Both are
backward-compatible additions a 1.1 reader handles and older files never carry;
see [§8 Evolution](#8-evolution).

A recording that exists in the wild makes this format a compatibility contract, so
the rules in [§8 Evolution](#8-evolution) are as binding as the byte layouts.

## 1. Conventions

- **Endianness: little-endian, always.** Every integer field is little-endian
  regardless of the host. Readers never detect byte order and never rely on the
  host; the format decides. (Rationale: x86 and ARM are little-endian, so the
  common path is free.)
- **Offsets are u64; block lengths are u32.** A recording may exceed 4 GiB
  ([ADR-0001](adr/0001-recording-strategy.md) makes recordings large), so every
  *file offset* is 64-bit. A single block is a bounded batch and never gigabytes,
  so *block lengths* are 32-bit.
- **CRC-32** means the ISO 3309 / ITU-T V.42 CRC (reflected polynomial
  `0xEDB88320`), identical to zlib, gzip, PNG and ZIP — i.e. Python's
  `zlib.crc32`. A reader in another language uses that standard polynomial.
- **"MUST" / "MAY"** are used in the RFC 2119 sense.

## 2. File structure

```
┌────────────────────────┐  offset 0
│ Header (32 bytes)       │
├────────────────────────┤  offset 32
│ Block 0                 │  ┐
│ Block 1                 │  │ payload blocks, in write order:
│ …                       │  │ META first, then STRINGS / EVENTS /
│ Block k                 │  │ VALUES / KEYFRAMES as produced
├────────────────────────┤  ┘
│ INDEX block             │  written last: locates every block above
├────────────────────────┤
│ EOCD (32 bytes)         │  present ⇔ file was closed cleanly
└────────────────────────┘  EOF
```

A file is **append-only**: blocks are written in order and never rewritten. The
INDEX and EOCD are written once, at clean close. Their **absence** is the signal
that the writer was killed mid-recording; see [§7 Reading](#7-reading).

## 3. Header (offset 0, 32 bytes)

| Offset | Size | Type | Field | Value |
|---:|---:|---|---|---|
| 0 | 11 | bytes | `magic` | `89 43 48 52 4F 4E 4F 0D 0A 1A 0A` (`\x89CHRONO\r\n\x1a\n`) |
| 11 | 2 | u16 | `version_major` | `1` |
| 13 | 2 | u16 | `version_minor` | `1` |
| 15 | 8 | u64 | `flags` | feature bitfield ([§8](#8-evolution)); `0` in 1.0 |
| 23 | 2 | u16 | `header_size` | `32` — offset at which the first block begins |
| 25 | 7 | — | *reserved* | zero |

The magic is modelled on PNG's: the high bit of the first byte makes the file
invalid UTF-8 (never mistaken for text), `CHRONO` is human-visible, and
`\r\n`…`\n` around `\x1a` is a canary for line-ending translation or DOS-EOF
truncation in transit. A reader MUST reject a file whose `magic` differs.

`header_size` is stored, not assumed: a future 1.x may append fixed header fields,
and a reader finds the body at `header_size` and ignores header bytes it does not
recognise.

## 4. Block framing

Every block is a 12-byte frame followed by its payload:

| Offset | Size | Type | Field |
|---:|---:|---|---|
| 0 | 4 | u32 | `payload_length` — bytes of payload that follow |
| 4 | 2 | u16 | `block_type` — see [§6](#6-block-types) |
| 6 | 2 | u16 | `block_flags` — how the payload is stored (e.g. compression) |
| 8 | 4 | u32 | `payload_crc32` — CRC-32 of the payload **as stored on disk** |
| 12 | `payload_length` | bytes | payload |

**Both length and CRC are required, and neither substitutes for the other:**

- The **length** *frames* the block. A reader can skip a whole block — including
  one whose `block_type` it does not understand — by advancing
  `12 + payload_length` bytes. Framing is what makes the format forward-compatible
  and scannable.
- The **CRC** proves the framed bytes are *intact*. A crash mid-write leaves a
  final block whose `payload_length` may look plausible but whose payload is short
  or garbage. The CRC turns that into a detectable torn write instead of data.

Two rules a reader MUST follow, in order:

1. Before reading a payload, check `payload_length ≤ (file_size − current_offset −
   12)`. A crash can write a garbage length; without this check a reader could try
   to allocate gigabytes or read past EOF.
2. Verify `payload_crc32` over the payload bytes **before** decoding them. Never
   interpret an unverified payload.

The CRC covers the payload *as stored* — after compression, if
`block_flags` sets `COMPRESSED_ZSTD` (1.1). So the integrity check is on the
exact bytes on disk; a reader verifies, then decompresses, then decodes.

**Compression frame (1.1).** When `block_flags` sets `COMPRESSED_ZSTD`, the payload
(for EVENTS, the payload *after* its uncompressed `u32 event_count` — see §6.3) is a
self-describing frame:

| Offset | Size | Type | Field |
|---:|---:|---|---|
| 0 | 1 | u8 | `codec` — `0` raw (incompressible fallback), `1` zstd |
| 1 | 4 | u32 | `raw_length` — decompressed byte length |
| 5 | … | bytes | zstd stream, or the raw bytes if `codec = 0` |

There is **no embedded or external dictionary**: each frame is a standalone zstd
stream, so a recording is self-contained. (A trained dictionary was measured and
rejected — it is a net loss at these block sizes; see `benchmarks/RESULTS.md`.) A
reader MUST bound decompression by `raw_length` and by an absolute ceiling *before*
allocating: a hostile frame declaring 4 GB must raise, never OOM. The `raw` codec
exists so an incompressible block is stored at `+5` bytes, never expanded — a format
that can only grow data is broken.

## 5. EOCD — end of central directory (last 32 bytes)

Written once, at clean close, at the very end of the file:

| Offset from record start | Size | Type | Field |
|---:|---:|---|---|
| 0 | 8 | u64 | `index_offset` — file offset of the INDEX block's frame |
| 8 | 8 | u64 | `index_length` — total bytes of the INDEX block (frame + payload) |
| 16 | 4 | u32 | `index_crc32` — CRC-32 of the INDEX block's payload |
| 20 | 4 | u32 | `flags` — `EocdFlag`; bit 0 = `TRUNCATED` (events were dropped) |
| 24 | 8 | bytes | `magic` = `CHRONEND` |

A reader seeks to `EOF − 32`, reads this record, and checks `magic`. If it matches
and `index_crc32` matches the INDEX payload, the file was **closed cleanly** and
the reader jumps straight to the INDEX. If not, the file is treated as **crashed**
and recovered by scanning ([§7](#7-reading)). `index_crc32` is redundant with the
INDEX block's own CRC on purpose: it lets a reader trust the pointer before
seeking to it.

`flags.TRUNCATED` distinguishes a *cleanly closed but incomplete* recording (events
dropped under backpressure — a slow or full disk) from a complete one and from a
crash (no EOCD at all). It is **informational, never gating**: a reader shows the UI
"incomplete" but still reads the file. The revision that added this field, and the
decision to *derive* the total event count from the EVENTS blocks rather than store
it here, were made on day 12 when implementing the writer surfaced the need — a
deliberate change to an unshipped format, per "the spec is the boss".

## 6. Block types

`block_type` is a u16. The top bit (`0x8000`, `OPTIONAL_BLOCK_BIT`) marks a block
**optional**: a reader that does not recognise an optional type MUST skip it (using
the length) and carry on. An unrecognised **required** type (`0x0001`–`0x7FFF`)
means the file uses a feature the reader lacks, and it MUST refuse the file rather
than guess. Tag values are permanent and never reused.

| Tag | Name | Required? | Payload |
|---:|---|---|---|
| `0x0001` | `META` | required | recording metadata ([§6.1](#61-meta)) |
| `0x0002` | `STRINGS` | required | interning tables ([§6.2](#62-strings)) |
| `0x0003` | `EVENTS` | required | one columnar batch of events ([§6.3](#63-events)) |
| `0x0004` | `VALUES` | required | content-addressed value pool ([§6.4](#64-values)) |
| `0x0005` | `INDEX` | required | block directory ([§6.5](#65-index)) |
| `0x8001` | `KEYFRAMES` | optional | full-state snapshots (day 15) |

`block_flags` (u16) defines one bit, `COMPRESSED_ZSTD = 0x0001` (§4): the payload is a
zstd compression frame. A reader that does not implement it MUST refuse the block
rather than read the frame as data — unlike an EOCD flag, a block-storage flag is
gating. In 1.1 the writer sets it on EVENTS and VALUES blocks; META and INDEX stay
uncompressed (both are tiny and read at open).

The framing in §3–§6 is fully normative. The internal encoding of the four data
payloads below reached its final form over 1.0→1.1 (columns day 12, msgpack values +
compression day 14); each is length- and count-prefixed so those refinements were
additive, never a reframing.

### 6.1 META

A msgpack map (day 14) with at least these string keys: `chronotrace_version`,
`format_version` (`[major, minor]`), `python_version`, `platform`,
`created_unix_ns`, and `config` (the resolved `RecorderConfig`). Written first so a
reader learns what it holds before touching the body. The total **event count is
not stored** — it is the sum of the EVENTS blocks' own counts, and the format does
not store what it can derive (nor could it, honestly, in a block written before any
event exists). Until the msgpack codec lands (day 14), a file carries an empty map
(`0x80`); the config snapshot is written then.

### 6.2 STRINGS

The recorder's interning tables, so events can carry small integer ids. Payload:
a u8 `table_count`, then that many tables; each table is a u32 `entry_count`
followed by `entry_count` entries, each a u32 `length` and that many UTF-8 bytes.
Table order is fixed: `0` filenames, `1` code-object descriptors, `2` variable
names, `3` exception type names. An id is an index into its table.

### 6.3 EVENTS

One block holds up to **N events** (default `N = 65536`; the block-size experiment
is day 18) stored **column-major**: all `seq` together, then all `kind`, and so
on. This is the core design decision — see
[ADR-0004](adr/0004-chrono-file-format.md) for why columnar, with the measured
7–12× compression win over row.

Payload:

1. u32 `event_count` (`≤ N`).
2. Ten columns, in this exact order (the field order of `recorder.events.Event`):
   `seq`, `kind`, `timestamp_ns`, `thread_id`, `frame_id`, `code_id`, `lineno`,
   `name_id`, `value_ref`, `exc_type_id`. `None` is encoded as `-1` (these fields
   are otherwise non-negative).
3. Each column is: a u8 `codec`, a u32 `byte_length`, then `byte_length` bytes.

When the block is compressed (1.1), the `u32 event_count` stays **uncompressed** at
the front of the payload and only the columns after it form the compression frame
(§4). This lets a reader read a block's event count — enough to build its seq index —
without decompressing the block, which is what keeps opening a large file cheap.

Codecs (a writer picks the smallest per column; a reader implements all three):

- `0x00` **raw** — `event_count` little-endian `int64` values. Always valid and the
  fallback; wins on incompressible columns.
- `0x01` **rle** — run-length `(value, count)` pairs as little-endian `int64`.
  Crushes constant columns (`thread_id`, `kind`, the `-1` runs of `name_id`).
- `0x02` **delta-rle** — run-length of the *consecutive differences*. Crushes
  monotonic and constant-stride columns: `seq` is `+1`, so its deltas are one long
  run of `1`. (Plain delta is not a codec: a run of `1`s is still a run of `int64`s
  and does not shrink pre-zstd, so the run-length is composed in. zstd, day 14,
  compresses whatever survives.)

### 6.4 VALUES

The content-addressed value pool ([ADR-0003](adr/0003-dedup-correctness.md)): each
distinct captured value stored once, addressed by the `value_ref` events carry
(the index into this pool). Payload: a u32 `value_count`, a directory of
`value_count` `(u64 offset, u32 length)` pairs (offsets relative to the start of the
value-bytes region, immediately after the directory), then the concatenated encoded
values. **Values are msgpack (1.1) restricted to the capturer's closed type set** —
the tagged shapes of [§9 Security](#9-security) plus the atoms; `complex`, which
msgpack has no native type for, is carried as `{"$": "complex", "real", "imag"}`. A
`value_ref` in an event is an index into the directory.

The pool is written **once, at close**, from the whole recording's deduplicated
values, so it is normally a single VALUES block. A reader MUST bounds-check every
directory `(offset, length)` against the block before slicing — a recording arrives
from a stranger, so a directory entry pointing past the block is a corrupt file, not
a read. The write side verifies on every repeat that a re-seen content hash carries
byte-identical content, turning a hash collision (or an upstream canonicalisation
bug) into a loud failure at write rather than a wrong value at read.

### 6.5 INDEX

The footer that makes clean-open O(1). Payload: `INDEX_ENTRY` records back to
back, one per block in the file, in file order:

| Size | Type | Field |
|---:|---|---|
| 2 | u16 | `block_type` |
| 8 | u64 | `offset` — file offset of that block's frame |
| 4 | u32 | `length` — total bytes of that block (frame + payload) |

A reader groups entries by `block_type`; several `EVENTS` entries are normal. The
INDEX does not list itself (the EOCD locates it).

## 7. Reading

**Clean open.** Read the last 32 bytes as an EOCD. If `magic == CHRONEND` and the
INDEX payload's CRC equals `index_crc32`, seek to `index_offset`, read and
CRC-verify the INDEX block, and parse its entries. You now have every block's
location and can mmap the file for random access by `seq`.

**Crashed / still-being-written open.** If the EOCD is absent or invalid, recover
by **scanning** from offset `header_size`:

```
pos = header_size
while file has ≥ 12 bytes at pos:
    read the 12-byte frame at pos
    if payload_length > bytes_remaining_after_frame:  break   # torn tail
    if crc32(payload) != payload_crc32:               break   # torn tail
    accept this block; index it in memory
    pos += 12 + payload_length
```

The accepted blocks are the recoverable prefix; a partially-written final block is
detected and dropped. This same path serves live tailing of an in-progress
recording.

**Crash-recovery guarantee, in one sentence:** *every block whose 12-byte frame is
present and whose payload CRC validates is readable; a crash truncates the file to
some prefix, losing at most the final partially-written block and the footer, and
never corrupting an earlier block.*

## 8. Evolution

- `version_major` gates compatibility. A reader MUST refuse a file whose
  `version_major` exceeds its own.
- `version_minor` marks backward-compatible additions. A reader MAY open a higher
  minor: new **optional** blocks (`0x8000+`) are skippable, and new fixed header
  fields live past `header_size`.
- The header `flags` bitfield declares file-wide capabilities (a codec used
  throughout, say). A reader MUST refuse a file that sets a `flags` bit it does not
  understand — unlike an optional *block*, a file-wide flag cannot be skipped.
- A reader MUST NOT guess at anything it does not recognise.

This format **will** change — keyframes (day 15) are still planned. That is fine
because the mechanism is defined now: a new capability is a new optional block type
or a new minor version, and every older reader keeps working. The one thing that is
expensive — a `version_major` bump — is reserved for a change that cannot be made any
other way.

**1.1 (day 14)** exercised this mechanism for real: per-block zstd compression and
msgpack values were added by activating a reserved block flag and filling in two
payload encodings, then bumping the minor. No framing changed, so a 1.1 reader opens
1.0 files unchanged (no block sets the compression flag). The change was disciplined
in exactly the way this section requires — a reserved bit and a minor bump, not a
silent reinterpretation of existing bytes.

## 9. Security

**`pickle` is banned at the format level.** Opening a `.chrono` file is a pure data
operation: parse framing, verify CRCs, decode msgpack values from a **closed type
registry** (the tagged shapes `recorder.capture` emits — `str`, `bytes`, `list`,
`dict`, `obj`, `cycle`, `depth`, `budget`, `redacted` — plus the atoms). No code
path constructs an arbitrary object, evaluates a string, or imports a named type.

This is a spec-level guarantee, not an implementation detail, because recordings
are shared in bug reports and opening a stranger's file is the normal workflow. A
malicious `.chrono` can at worst be *malformed* — rejected by CRC or schema
validation — never *executable*. A single pickle `__reduce__` would turn "open this
recording" into "run this attacker's code"; that door is closed by construction.

## 10. Durability

The writer does **not** `fsync` per block. It writes through the OS page cache and
`fsync`s once, immediately before the EOCD, so a *cleanly closed* file is durable.
The crash guarantee in §7 does not depend on `fsync`: whatever bytes reached disk
are self-framed and CRC-checked, so the prefix is structurally valid either way. A
`kill -9` loses nothing (the page cache survives process death); only a power loss
or kernel panic can drop unflushed tail blocks, and losing the last few events of a
*debug artifact* is an acceptable price for not halving the traced program's speed.
A debugger that fsynced every block would have that trade backwards.

## 11. Edge cases

| Case | Behaviour |
|---|---|
| Empty recording (0 events) | Valid: header + META + INDEX + EOCD, no EVENTS block. |
| Dropped events, clean close | Valid; EOCD `flags.TRUNCATED` set. Reader shows "incomplete". |
| In-progress (no EOCD) | Open read-only via the scan path; recover the written prefix. |
| Newer `version_major` | Refuse, loudly. |
| Truncated file | Scan stops at the first short/failed-CRC frame; prefix is intact. |
| Valid header, garbage body | Scan stops at the first frame that fails the length or CRC check. |
| File > 4 GiB / 100 GiB | Fine: offsets are u64, reads are mmap/streamed, nothing assumes RAM-fit. |
| Unknown optional block | Skip it (via length) and continue. |
| Unknown required block / flag | Refuse the file. |

## 12. A minimal valid file

103 bytes: header + one `META` block (payload = msgpack empty map `0x80`) + an
`INDEX` locating it + EOCD. Emitted by `ChronoWriter` with zero events, CRCs real,
and pinned byte-for-byte by `tests/store/test_writer.py`:

```
0000  89 43 48 52 4f 4e 4f 0d 0a 1a 0a 01 00 01 00 00   .CHRONO.........
0010  00 00 00 00 00 00 00 20 00 00 00 00 00 00 00 00   ....... ........
0020  01 00 00 00 01 00 00 00 ad 6c ba 3f 80 0e 00 00   .........l.?....
0030  00 05 00 00 00 94 9b ef a6 01 00 20 00 00 00 00   ........... ....
0040  00 00 00 0d 00 00 00 2d 00 00 00 00 00 00 00 1a   .......-........
0050  00 00 00 00 00 00 00 94 9b ef a6 00 00 00 00 43   ...............C
0060  48 52 4f 4e 45 4e 44                              HRONEND
```

Reading it: `magic` at `0x00`; `version_major=1` at `0x0B`, `version_minor=1` at
`0x0D`; `header_size=32` (`0x20`) at `0x17`. The META frame at `0x20` —
`payload_length=1`, `type=0x0001`, `crc32=3fba6cad`, payload `80` (an empty msgpack
map; too small to compress, so it is stored uncompressed with no flag). The INDEX
frame at `0x2D` — `payload_length=14`, `type=0x0005`, one entry
`(type=META, offset=32, length=13)`. The EOCD at `0x47` — `index_offset=45`,
`index_length=26`, `index_crc32=a6ef9b94`, `flags=0`, `magic=CHRONEND`.
