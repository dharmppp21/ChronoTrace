"""A reporting helper that computes an average. It returns a number that looks fine.

For the dogfooding session (day 31): the bug is not described here on purpose -- it is found
with queries, not by reading. The expected answer for the input below is 25.0.
"""

from __future__ import annotations


def average(nums: list[int]) -> float:
    total = 0
    for i in range(len(nums) - 1):
        total += nums[i]
    return total / len(nums)


def main() -> float:
    return average([10, 20, 30, 40])


if __name__ == "__main__":
    print(main())
