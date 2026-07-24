"""A loop that hits one line many times with a changing variable -- for breakpoint tests.

`scan` runs the line `x = i * i` once per iteration with `i` counting up, so a conditional
retroactive breakpoint on that line has many candidates to filter -- and a live `pdb`
breakpoint on the same line has exactly the same. The day-30 oracle test asserts the two
find the identical set of instants.

Line numbers are load-bearing: the breakpoint line is `x = i * i`.
"""

from __future__ import annotations


def scan(n: int) -> int:
    total = 0
    for i in range(n):
        x = i * i  # <- the breakpoint line; conditions use `i` and `x`
        total += x
    return total


def main() -> int:
    return scan(20)


if __name__ == "__main__":
    print(main())
