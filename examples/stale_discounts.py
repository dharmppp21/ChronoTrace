"""Three orders, one shared list, and a discount that silently stops applying.

Run this and nothing crashes. There is no traceback to read, no line number to jump to --
just a number that is quietly wrong on the third order, and right on the first two. That is
the shape of bug this tool exists for: the symptom is far away from the cause, and the cause
already happened.

    python examples/stale_discounts.py
    order 1: 108.0
    order 2: 170.0
    order 3: 120.0      <- should be 108.0

The third customer used the same coupon as the first and got charged full price. Nothing in
the output says why, and re-running gives the same answer with no more information.

Record it and look backwards instead:

    chronotrace record --ui examples/stale_discounts.py
    chronotrace query --watch _applied

The `--watch` shows a list that keeps growing between orders when it should be empty at the
start of each one. That is the whole bug, visible as history rather than as a guess.
"""

from __future__ import annotations

DISCOUNTS = {"SPRING": 0.90, "BULK": 0.85, "LOYALTY": 0.95}


def checkout(items: dict[str, float], codes: list[str], _applied: list[str] = []) -> float:  # noqa: B006 -- the mutable default IS the bug this example teaches; fixing it removes the lesson
    """The total for one order, applying each discount code at most once.

    The bug lives in the signature. A default argument is evaluated ONCE, when the function is
    defined -- not on every call. So every order shares the same `_applied` list, and "apply
    each code at most once" quietly means "once ever, across all customers" instead of "once
    per order". The first customer to use a coupon consumes it for everybody.

    It is a plausible piece of code. The guard looks like ordinary defensive programming, the
    tests probably passed (one order at a time is fine), and the failure needs *two* orders
    sharing a code before it shows -- by which point the call that broke it is long gone.
    """
    total = sum(items.values())
    for code in codes:
        if code in _applied:
            continue  # "already used" -- but by whom?
        _applied.append(code)
        total *= DISCOUNTS[code]
    return round(total, 2)


def main() -> None:
    orders = [
        ({"widget": 100.0, "gasket": 20.0}, ["SPRING"]),
        ({"widget": 200.0}, ["BULK"]),
        # A different customer, same coupon. This is the one that comes out wrong.
        ({"widget": 100.0, "gasket": 20.0}, ["SPRING"]),
    ]
    for number, (items, codes) in enumerate(orders, start=1):
        print(f"order {number}: {checkout(items, codes)}")


main()
