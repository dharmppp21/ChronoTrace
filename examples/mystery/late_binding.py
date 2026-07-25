"""Builds a list of multiplier functions. Picking the second one gives the wrong factor.

Dogfooding (day 31): the bug is not described here. `main` builds multipliers for factors
2, 3, 4, takes the *second* (expected: multiply by 3), and applies it to 10 -- expecting 30.
"""

from __future__ import annotations

from collections.abc import Callable


def make_multipliers() -> list[Callable[[int], int]]:
    result = []
    for factor in (2, 3, 4):
        result.append(lambda x: x * factor)  # noqa: B023 -- binding the loop var IS the bug
    return result


def main() -> int:
    triple = make_multipliers()[1]
    return triple(10)


if __name__ == "__main__":
    print(main())
