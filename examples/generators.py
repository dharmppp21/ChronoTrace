"""Frames that suspend and resume -- the counter-example that killed the stack.

Line numbers are load-bearing: tests/recorder/test_generators.py asserts on them.

`interleaved_generators` is the important one. Two generators of the SAME function
are alive at once and take turns. A stack says a frame is entered once and exited
once, LIFO; here F0 leaves, F1 enters, F0 comes back. No stack models that, which
is why frames.py is a registry.

`async_gather` is the same shape with the volume turned up: coroutines are
generators underneath, so `await` suspends a frame exactly as `yield` does, and
`gather` keeps several suspended at once. This is why `seq` is a global clock --
with frames interleaving, "what happened next" has no per-frame answer.
"""

import asyncio
from collections.abc import Iterator


def numbers(n: int) -> Iterator[int]:
    # `yield from range(n)` would be tidier Python and the wrong demo: delegation
    # is a different frame situation from a plain generator, and this file exists
    # to exercise the plain one. The explicit loop also gives the LINE events the
    # golden stream asserts on.
    for i in range(n):  # noqa: UP028
        yield i


def squares(source: Iterator[int]) -> Iterator[int]:
    for value in source:
        yield value * value


def pipeline() -> int:
    """A generator feeding a generator: two suspended frames, one consumer."""
    return sum(squares(numbers(4)))


def interleaved_generators() -> list[int]:
    """Two live generators of the same code object, taking turns.

    The stack model cannot represent this. The registry can.
    """
    a, b = numbers(3), numbers(3)
    return [next(a), next(b), next(a), next(b)]


def abandoned_generator() -> int:
    """A generator dropped before exhaustion.

    Its frame still exits: CPython throws GeneratorExit in during collection,
    producing RAISE -> EXCEPTION_HANDLED -> RERAISE -> PY_UNWIND. Measured, and
    the reason the registry cannot leak.
    """
    gen = numbers(100)
    first = next(gen)
    del gen
    return first


async def _slow_double(n: int) -> int:
    await asyncio.sleep(0)
    return n * 2


async def _gather() -> list[int]:
    return list(await asyncio.gather(*(_slow_double(i) for i in range(3))))


def async_gather() -> list[int]:
    """Several coroutine frames suspended at once, resuming out of order."""
    return asyncio.run(_gather())


def main() -> dict[str, object]:
    return {
        "pipeline": pipeline(),
        "interleaved": interleaved_generators(),
        "abandoned": abandoned_generator(),
        "async": async_gather(),
    }


if __name__ == "__main__":
    print(main())
