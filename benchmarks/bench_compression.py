"""Day 14: compression ratio, throughput, and the two justifications the design owes.

Run: `python benchmarks/bench_compression.py`

Four real workloads are recorded *with value capture* so the numbers include the
value pool, not just control flow. Then:

  * **Bytes per event** (the README number): the shipped columnar+zstd writer against
    the same events uncompressed.
  * **Columnar earns its place** (day 12): columnar+zstd vs a naive row layout+zstd,
    proving the columns still matter *after* a general compressor runs.
  * **The dictionary does NOT earn its place** (day 14): an embedded trained dictionary
    on a real single VALUES block, shown net-negative -- which is why none is shipped.
  * **Throughput**: compress / decompress MB/s at the shipped level.
"""

from __future__ import annotations

import statistics
import struct
import sys
import time
from array import array
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "spikes"))

import zstandard as zstd  # noqa: E402
from workloads import WORKLOADS  # type: ignore[import-not-found]  # noqa: E402

from chronotrace.recorder import MemorySink, Recorder  # noqa: E402
from chronotrace.recorder.scope import Scope  # noqa: E402
from chronotrace.store.columnar import encode_events  # noqa: E402
from chronotrace.store.compression import DEFAULT_LEVEL, compress, decompress  # noqa: E402
from chronotrace.store.valuepool import ValuePoolWriter  # noqa: E402

WORKLOAD_NAMES = ("tight_loop", "fib_recursive", "json_pipeline", "io_bound")
_BLOCK = 65536


def _record(name: str) -> tuple[list, list]:
    fn = WORKLOADS[name]
    fn()  # warm imports so they are out of scope
    sink = MemorySink()
    recorder = Recorder(sink, capture_values=True, scope=Scope(include=["*"]))
    with recorder:
        fn()
    # The pool has no public "give me every value" accessor -- a reader resolves one
    # ref at a time. A benchmark legitimately reaches in for the whole thing.
    pool_values = recorder._values._values
    return sink.events, pool_values


def _event_blocks(events: list) -> list[bytes]:
    return [encode_events(events[i : i + _BLOCK]) for i in range(0, len(events), _BLOCK)]


def _pool_section(pool_values: list) -> bytes:
    pool = ValuePoolWriter()
    for value in pool_values:
        pool.add(value)
    return pool.encode()


def _row_encode(events: list) -> bytes:
    """A naive row layout: each event's ten fields as int64s, event after event.

    The thing columnar encoding replaced -- the baseline that proves columns earn keep.
    """
    fields = ("seq", "kind", "timestamp_ns", "thread_id", "frame_id", "code_id", "lineno")
    flat = array("q")
    for e in events:
        flat.extend(int(getattr(e, f)) for f in fields)
        vref = e.value_ref if e.value_ref is not None else -1
        flat.extend((e.name_id or -1, vref, e.exc_type_id or -1))
    if sys.byteorder != "little":
        flat.byteswap()
    return flat.tobytes()


def _bytes_per_event() -> None:
    print("Bytes per event -- four workloads (columnar + zstd, values included)")
    print(
        f"{'workload':<15} {'events':>9} {'values':>8} "
        f"{'raw B/ev':>9} {'zstd B/ev':>10} {'ratio':>7}"
    )
    print("-" * 62)
    for name in WORKLOAD_NAMES:
        events, pool_values = _record(name)
        n = max(len(events), 1)
        blocks = _event_blocks(events)
        section = _pool_section(pool_values)
        raw = sum(len(b) for b in blocks) + len(section)
        comp = sum(len(compress(b)) for b in blocks) + len(compress(section))
        print(
            f"{name:<15} {len(events):>9,} {len(pool_values):>8,} "
            f"{raw / n:>9.1f} {comp / n:>10.1f} {raw / max(comp, 1):>6.1f}x"
        )


def _columnar_earns_its_place() -> None:
    events, _ = _record("json_pipeline")
    block = events[:_BLOCK]
    col = compress(encode_events(block))
    row = compress(_row_encode(block))
    print("\nColumnar earns its place (day 12): one EVENTS block, both then zstd-compressed")
    print(f"  row layout   + zstd : {len(row):>8,} B")
    print(f"  columnar     + zstd : {len(col):>8,} B   ({len(row) / len(col):.1f}x under row)")


def _dictionary_does_not_earn_its_place() -> None:
    _, pool_values = _record("json_pipeline")
    section = _pool_section(pool_values)
    blobs = _pool_blobs(pool_values)
    no_dict = len(compress(section))
    try:
        zd = zstd.train_dictionary(8 * 1024, blobs)
        with_dict = len(zstd.ZstdCompressor(level=DEFAULT_LEVEL, dict_data=zd).compress(section))
        dict_cost = len(zd.as_bytes())
        net = no_dict - (with_dict + dict_cost)
    except zstd.ZstdError:
        with_dict = dict_cost = net = 0
    verdict = "ship it" if net > 0 else "not shipped (a net loss)"
    print("\nThe dictionary does NOT earn its place (day 14): single VALUES block")
    print(f"  no dictionary       : {no_dict:>8,} B")
    print(f"  + 8 KB trained dict : {with_dict + dict_cost:>8,} B  ({with_dict}+{dict_cost} dict)")
    print(f"  net                 : {net:+,} B  -> {verdict}")


def _pool_blobs(pool_values: list) -> list[bytes]:
    pool = ValuePoolWriter()
    for value in pool_values:
        pool.add(value)
    section = pool.encode()
    (count,) = struct.unpack_from("<I", section, 0)
    dir_end = 4 + count * 12
    out = []
    for i in range(count):
        offset, length = struct.unpack_from("<Q I", section, 4 + i * 12)
        out.append(section[dir_end + offset : dir_end + offset + length])
    return out


def _throughput() -> None:
    events, _ = _record("tight_loop")
    data = encode_events(events[:_BLOCK])
    ct = statistics.median(_time(lambda: compress(data)) for _ in range(5))
    frame = compress(data)
    dt = statistics.median(_time(lambda: decompress(frame)) for _ in range(5))
    print(f"\nThroughput (level {DEFAULT_LEVEL}, one {len(data):,}-byte EVENTS block)")
    print(f"  compress   : {len(data) / ct / 1e6:>7.0f} MB/s")
    print(f"  decompress : {len(data) / dt / 1e6:>7.0f} MB/s")


def _time(fn: object) -> float:
    t0 = time.perf_counter()
    fn()  # type: ignore[operator]
    return time.perf_counter() - t0


def main() -> int:
    _bytes_per_event()
    _columnar_earns_its_place()
    _dictionary_does_not_earn_its_place()
    _throughput()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
