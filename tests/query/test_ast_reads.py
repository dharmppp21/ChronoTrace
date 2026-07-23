"""AST read extraction: the tricky lines it must get right, and the files it must refuse.

The refusal is the important half. A heuristic that analyses a changed source file would
confidently name the inputs of a line that no longer exists -- so a hash mismatch, a missing
file, and a missing recorded hash all raise rather than guess.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from chronotrace.query._ast_reads import SourceUnavailable, reads_on_line

SOURCE = """\
a = b + c
d = [x for x in items if x > threshold]
e = (
    first
    + second
)
f = obj.attr.deep
g = h.method(arg)
i = 1
j = (k := compute(m))
"""


def _write(tmp_path: Path) -> tuple[str, str]:
    """Write the sample source and return `(path, the sha256 of the bytes actually on disk)`.

    Hash the bytes read back, not `SOURCE`: on Windows `write_text` would translate `\\n` to
    `\\r\\n`, and the recorder hashes on-disk bytes -- so this mirrors the real pipeline.
    """
    path = tmp_path / "sample.py"
    path.write_text(SOURCE, encoding="utf-8", newline="")
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def test_simple_binary_op_reads_both_operands(tmp_path: Path) -> None:
    path, digest = _write(tmp_path)
    assert reads_on_line(path, 1, digest) == frozenset({"b", "c"})


def test_comprehension_reads_the_iterable_and_condition(tmp_path: Path) -> None:
    """`items` and `threshold` are read; the loop target `x` is a Store, not a read."""
    path, digest = _write(tmp_path)
    reads = reads_on_line(path, 2, digest)
    assert {"items", "threshold"} <= reads


def test_a_multiline_expression_is_attributed_per_physical_line(tmp_path: Path) -> None:
    """`first` is on line 4, `second` on line 5 -- each name to the line it physically sits on."""
    path, digest = _write(tmp_path)
    assert reads_on_line(path, 4, digest) == frozenset({"first"})
    assert reads_on_line(path, 5, digest) == frozenset({"second"})


def test_a_chained_attribute_reads_only_its_root(tmp_path: Path) -> None:
    """`obj.attr.deep` reads `obj`; `attr` and `deep` are attribute names, not locals."""
    path, digest = _write(tmp_path)
    assert reads_on_line(path, 7, digest) == frozenset({"obj"})


def test_a_method_call_reads_the_receiver_and_arguments(tmp_path: Path) -> None:
    path, digest = _write(tmp_path)
    assert reads_on_line(path, 8, digest) == frozenset({"h", "arg"})


def test_a_line_that_reads_nothing_is_empty_not_an_error(tmp_path: Path) -> None:
    """`i = 1` reads no name -- an empty set, distinct from a `SourceUnavailable` refusal."""
    path, digest = _write(tmp_path)
    assert reads_on_line(path, 9, digest) == frozenset()


def test_a_walrus_reads_its_value_not_its_target(tmp_path: Path) -> None:
    """`j = (k := compute(m))` reads `compute` and `m`; `k` is written, not read."""
    path, digest = _write(tmp_path)
    assert reads_on_line(path, 10, digest) == frozenset({"compute", "m"})


def test_a_changed_file_is_refused_not_analysed(tmp_path: Path) -> None:
    """The whole point: a hash that does not match the file on disk means refuse."""
    path, _ = _write(tmp_path)
    with pytest.raises(SourceUnavailable, match="changed"):
        reads_on_line(path, 1, "0" * 64)  # a hash that cannot match the real file


def test_no_recorded_hash_is_refused(tmp_path: Path) -> None:
    """Without a hash there is nothing to verify against -- refuse rather than trust the disk."""
    path, _ = _write(tmp_path)
    with pytest.raises(SourceUnavailable, match="no source hash"):
        reads_on_line(path, 1, None)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SourceUnavailable, match="cannot read"):
        reads_on_line(str(tmp_path / "gone.py"), 1, "0" * 64)
