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


def deep_chain() -> str:
    """Shape 5 (day 29): a five-deep explicit `__cause__` chain, root buried deepest.

    RuntimeError <- TypeError <- IndexError <- KeyError <- ValueError("root"). Walking the
    recorded chain from the surface RuntimeError must reach the ValueError, not stop early.
    Defined after `main` and deliberately *not* called from it, so the four-shape golden
    stream that `test_exceptions.py` pins is untouched.
    """
    try:
        try:
            try:
                try:
                    raise ValueError("root")
                except ValueError as e1:
                    raise KeyError("second") from e1
            except KeyError as e2:
                raise IndexError("third") from e2
        except IndexError as e3:
            raise TypeError("fourth") from e3
    except TypeError as e4:
        try:
            raise RuntimeError("the surface error") from e4
        except RuntimeError:
            return "deep"
