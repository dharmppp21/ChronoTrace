"""Two Sum, with the classic off-by-one-line bug: it passes the sample test and fails a hidden one.

The question (LeetCode #1): given a list of numbers and a target, return the indices of the two
numbers that add to the target. Each input has exactly one answer, and you may not use the same
element twice.

The solution below is the standard hash-map answer, and it is one line out of order. Run it:

    python examples/two_sum_dsa.py

    nums=[2, 7, 11, 15] target=9   -> (0, 1)   expected (0, 1)   OK
    nums=[3, 2, 4]      target=6   -> (0, 0)   expected (1, 2)   WRONG
    nums=[3, 3]         target=6   -> (0, 0)   expected (0, 1)   WRONG

Notice what makes this nasty: the *first* case is the one printed in every version of the
question, so it passes the test you actually run. It only breaks when one number happens to be
exactly half the target -- and then it returns `(0, 0)`, which is a perfectly plausible-looking
pair of indices rather than a crash.

Record it and look at what the map contained at the moment of the match:

    chronotrace record examples/two_sum_dsa.py
    chronotrace query --watch seen

You will see `seen` already containing the current number before the check runs -- so the
number pairs with *itself*.
"""

from __future__ import annotations


def two_sum(nums: list[int], target: int) -> tuple[int, int] | None:
    """Indices of the two numbers adding to `target`, or None.

    The bug: `seen[n] = i` happens BEFORE the lookup. The intended logic is "have I already
    seen the number that completes this pair?" -- a question about the numbers *before* this
    one. Storing first quietly changes it to "...including this one", so when `n` is exactly
    half of `target`, the number completes itself and the function returns the same index twice.

    Correct order is: look first, then store.
    """
    seen: dict[int, int] = {}
    for i, n in enumerate(nums):
        seen[n] = i  # <- one line too early
        complement = target - n
        if complement in seen:
            return (seen[complement], i)
    return None


def main() -> None:
    cases = [
        ([2, 7, 11, 15], 9, (0, 1)),  # the sample from the question -- passes
        ([3, 2, 4], 6, (1, 2)),  # 3 is half of 6 -- breaks
        ([3, 3], 6, (0, 1)),  # duplicates -- breaks
    ]
    for nums, target, expected in cases:
        got = two_sum(nums, target)
        verdict = "OK" if got == expected else "WRONG"
        print(f"nums={nums} target={target} -> {got}  expected {expected}  {verdict}")


main()
