"""The four shapes an exception takes, each of which the recorder must tell apart.

Line numbers are load-bearing: tests/recorder/test_exceptions.py asserts against
them.

The distinction this file exists to prove: CPython fires RAISE in *every* frame an
exception crosses, not only where it started. `deep_raise` produces RAISE three
times for one exception. Only the first is the origin, and "jump to the origin"
is the query the whole exception model exists to serve.
"""

import contextlib


def _innermost() -> int:
    raise ValueError("born here")


def _middle() -> int:
    return _innermost()


def deep_raise() -> str:
    """Shape 1: raised deep, caught shallow. Three frames, ONE origin."""
    try:
        _middle()
    except ValueError:
        return "caught"
    return "unreachable"


def raise_from() -> str:
    """Shape 2: explicit chaining. `__cause__` is set."""
    try:
        raise KeyError("the underlying cause")
    except KeyError as exc:
        try:
            raise RuntimeError("the surface error") from exc
        except RuntimeError:
            return "chained"


def implicit_context() -> str:
    """Shape 3: implicit chaining. `__context__` is set, `__cause__` is not."""
    try:
        raise KeyError("first failure")
    except KeyError:
        try:
            raise RuntimeError("failure while handling the first")
        except RuntimeError:
            return "contexted"


def handled_in_place() -> str:
    """Shape 4: raised and caught in the SAME frame. No unwind happens at all.

    The frame never exits abnormally, so a model that only watched UNWIND would
    miss this exception entirely.
    """
    with contextlib.suppress(ZeroDivisionError):
        _ = 1 / 0
    return "handled"


def main() -> list[str]:
    return [deep_raise(), raise_from(), implicit_context(), handled_in_place()]


if __name__ == "__main__":
    print(main())
