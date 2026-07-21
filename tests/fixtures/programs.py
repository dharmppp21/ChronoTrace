"""Small programs the equivalence harness records, kept out of the harness's own package.

Deliberately here rather than beside the tests that use them. The harness scopes the
recorder to a directory, so a target function living next to `truth.py` would put the
observer inside its own scope -- an observer observing itself, whose `__enter__` runs
before the recorder exists. `truth.py` guards against that structurally as well; this
keeps the *scope* honest too, so tests exercise the real "record a directory of user
code" path instead of a special case.
"""

from __future__ import annotations

from typing import Any


def mutates_in_place() -> list[int]:
    """A list changed under a stable `id()` -- the shape of the day-8 dedup bug.

    Length never changes, so a cache keyed on anything but content hands back a stale
    reference and the debugger shows the value the list *used* to hold.
    """
    data = [1, 1, 1]
    data[0] = 2
    data[1] = 3
    return data


def holds_a_secret() -> str:
    """A local whose name marks it a secret, next to one that is not."""
    password = "hunter2"  # noqa: S105 -- being withheld is the point
    safe = "public"
    return safe + password[:0]


def deletes_a_local() -> int:
    """`del` on a live binding: invisible to the recorder today (issue #7)."""
    doomed = [1, 2, 3]
    kept = len(doomed)
    del doomed
    return kept


def holds_the_zoo(zoo: dict[str, Any]) -> int:
    """Bind hostile objects into a real frame so there is something to reconstruct.

    Cycles, a 10-million-element list, a 4 GB phantom buffer, objects that raise from
    `__repr__` or fabricate attributes. Capture is bounded by policy and the observer
    applies the same policy, so a truncated value must reconstruct as the *same*
    truncated value -- nothing here is forgiven for being large.
    """
    values = list(zoo.values())
    count = len(values)
    return count
