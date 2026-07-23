"""What names a source line *reads* -- the approximate half of value provenance.

**Exactly what this can and cannot see, stated before anything else.** ChronoTrace records
`VAR_WRITE`, never `VAR_READ` (a deliberate day-4 cost decision), so true dataflow is not in
the recording. This module recovers a *guess* at the inputs of a line by parsing its source
and collecting the names it loads. That guess is:

* right for the common case -- `total = a + b` reads `a` and `b`;
* blind through calls and attributes -- `x = f(y).z` reads `f` and `y`, and cannot see what
  `f` read inside itself or where `z` came from;
* only ever names *loaded on that physical line*, so a multi-line expression is handled
  per-line, not as a whole;
* **worthless, and worse than nothing, against a source file that changed since the
  recording** -- it would confidently name the inputs of a line that no longer exists.

That last point is why this refuses to run unverified. The recording stored each source
file's SHA-256 at record time (format 1.7); this re-hashes the file on disk and analyses it
only on an exact match. A missing hash, a changed file, an unreadable or unparseable one all
raise `SourceUnavailable` -- the provenance query then falls back to the exact write and says
the inputs could not be recovered, rather than presenting a guess against the wrong source.

Interface: `reads_on_line(path, lineno, expected_hash)` and `SourceUnavailable`.

It must never know: what a query does with the names. It returns identifiers.
"""

from __future__ import annotations

import ast
import hashlib
from functools import lru_cache
from pathlib import Path


class SourceUnavailable(Exception):
    """The source line cannot be analysed -- and why. The caller degrades to exact-only.

    Raised for every reason the guess would be untrustworthy: no recorded hash to verify
    against, the file changed since recording, the file is gone or unreadable, or it does
    not parse. Each is a message the provenance result can show verbatim.
    """


def reads_on_line(path: str, lineno: int, expected_hash: str | None) -> frozenset[str]:
    """The identifiers *loaded* on `path:lineno`, verified against `expected_hash`.

    Empty for a line that reads nothing (`x = 1`) or a line with no code (blank, comment) --
    an empty set is a real answer, distinct from the `SourceUnavailable` a changed file gets.

    Raises:
        SourceUnavailable: the source cannot be trusted or read (see the class).

    Complexity: O(nodes) on the first line queried for a file (one parse + walk, cached),
    O(1) per line after.
    """
    return _reads_map(path, expected_hash).get(lineno, frozenset())


@lru_cache(maxsize=128)
def _reads_map(path: str, expected_hash: str | None) -> dict[int, frozenset[str]]:
    """`lineno -> names loaded there` for one verified file. Parsed once, cached per file.

    Keyed by `(path, expected_hash)` so a changed file (new hash) never reuses a stale
    parse, and two recordings of the same untouched source share the work.
    """
    text = _verified_text(path, expected_hash)
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise SourceUnavailable(f"{path} does not parse: {exc}") from exc
    buckets: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        # Only Name *loads*: a Store target is a write, not a read, and the root of an
        # attribute or subscript chain (`bucket` in `bucket['x']`) is itself a Name load,
        # so collecting Names alone captures the locals a line consumed.
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            buckets.setdefault(node.lineno, set()).add(node.id)
    return {line: frozenset(names) for line, names in buckets.items()}


def _verified_text(path: str, expected_hash: str | None) -> str:
    """The file's text, only if its SHA-256 matches what the recording stored.

    Raises:
        SourceUnavailable: no hash, a mismatch, or the file cannot be read.
    """
    if expected_hash is None:
        raise SourceUnavailable(f"no source hash was recorded for {path}; cannot verify it")
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SourceUnavailable(f"cannot read {path}: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise SourceUnavailable(f"{path} has changed since the recording; refusing to analyse it")
    return data.decode("utf-8", "replace")
