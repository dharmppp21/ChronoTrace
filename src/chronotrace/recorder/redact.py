"""Keeps secrets out of the recording by refusing to read them -- a security file.

The rule, before any detail: redact *before* capture, never after
-----------------------------------------------------------------
A recording is a dump of live memory, so it contains whatever the program held --
passwords, tokens, API keys. "Capture then scrub" is worthless: the secret has
already been copied into our buffers, and any crash dump, partial flush, or
core file leaks it before the scrub runs. Redaction must prevent the *read*. So
the recorder checks the variable's name against these patterns and, on a match,
stores a marker **without ever calling `capture()` on the value**. The secret is
never read into our process's data structures at all.

Redaction is visible, not silent
---------------------------------
A redacted local emits `REDACTED` as its value, not nothing. A developer must be
able to tell "this was hidden" from "this never existed" -- a missing variable
looks like a bug in the recorder, a marked one looks like the security feature it
is. The variable's *name* is still recorded; only its value is withheld.

By name, not by value -- and the honest limitations
---------------------------------------------------
Matching is on the binding's name (`*password*`, `*token*`, ...), case-insensitive.
This is the right *default* because it has zero false positives on ordinary code,
is predictable, and is explainable to a security reviewer. Its limits, stated
plainly rather than pretended away:

* **A secret in a variable named `x` is captured.** Name-based detection cannot
  see it. Value-based scanning (entropy heuristics, key-format regexes) is a
  future opt-in (tracked for day 47), not a default -- false positives that
  silently drop real data are their own failure.
* **A nested secret is captured.** `config["password"]` binds the local `config`;
  redaction sees `config`, not the key inside it, and the whole dict (password
  included) is captured. Documented in `docs/configuration.md`; do not rely on
  name redaction for secrets held inside containers.

`*auth*` is the broadest default and will also match `author`/`oauth`; that
false-positive is accepted as the safe direction (hide too much, never too
little) and is called out in the docs.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

DEFAULT_PATTERNS: tuple[str, ...] = (
    "*password*",
    "*passwd*",
    "*secret*",
    "*token*",
    "*api_key*",
    "*apikey*",
    "*auth*",
    "*credential*",
)
"""Case-insensitive name globs redacted by default. Broad on purpose: the cost of
a false positive is a hidden non-secret (annoying), the cost of a false negative
is a leaked secret (a breach)."""

REDACTED: dict[str, str] = {"$": "redacted"}
"""The marker stored in place of a redacted value.

A captured-representation shape (tagged dict), so it flows through the value pool,
dedup and serialisation like any other value -- every redaction shares one pooled
reference. Treated as immutable; never mutated in place.
"""


class Redactor:
    """Decides, by name, whether a binding's value must never be read.

    Holds no state beyond its patterns and touches no user code: matching is on
    the *name* string, so unlike a value check it cannot trigger a `__eq__` or
    `__repr__`. That keeps redaction on the right side of the no-user-code rule
    even though it runs on every local.
    """

    __slots__ = ("_patterns",)

    def __init__(self, patterns: Iterable[str] = DEFAULT_PATTERNS) -> None:
        """Build a redactor.

        Args:
            patterns: fnmatch globs matched case-insensitively against local
                names. Lower-cased once here so matching does not re-fold per call.
        """
        self._patterns = tuple(p.lower() for p in patterns)

    def should_redact(self, name: str) -> bool:
        """Whether a local called `name` must be redacted (its value never read).

        Args:
            name: the local variable's name.

        Returns:
            True if any pattern matches, case-insensitively.

        Complexity: O(patterns), a handful of fnmatch calls on a short string.
        Runs once per local per line; measured negligible against capture cost.
        """
        low = name.lower()
        return any(fnmatchcase(low, p) for p in self._patterns)
