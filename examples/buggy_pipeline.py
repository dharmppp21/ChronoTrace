"""THE demo bug: a total that is wrong because a dict was shared 300 events ago.

This is the program ChronoTrace exists to make debuggable, so it is built to be a
*good* demo bug -- which is a specific thing, not just "a bug":

* **Cause and symptom are far apart in time.** The mistake happens once, at line
  `dict.fromkeys(...)`, before any record is processed. The symptom -- a regional
  total that is obviously wrong -- is only visible after all 90 records are
  aggregated, ~360 events later. A breakpoint on the symptom shows you a correct
  line doing a correct `+=`; the lie is upstream and already gone.
* **Cause and symptom are far apart in code.** The bug is in initialisation; the
  wrong number surfaces in reporting. Nothing at the crime scene points home.
* **It does not crash.** It prints a plausible-looking report with wrong numbers,
  which is worse than a traceback -- there is no line to start from.

Why it is genuinely annoying in `pdb`: stepping forward, every `+=` looks right.
To find it you must already suspect aliasing and think to compare `id()` of the
three buckets -- i.e. you must have guessed the answer before the tool helps. The
question a time-travel debugger answers directly is "who *last* wrote to
`totals['south']['sales']`?", and the answer -- *a north record* -- is the whole
bug in one line.

The bug
-------
`dict.fromkeys(REGIONS, {"sales": 0.0, "orders": 0})` gives every region **the same
dict object**, because the single default is evaluated once and shared. Writing to
one region writes to all three, so each region ends up holding the grand total.
The fix is a per-key dict (`{r: {...} for r in REGIONS}`); it is deliberately not
applied, because this file's job is to stay broken.
"""

from __future__ import annotations

REGIONS = ("north", "south", "east")

# 90 orders, 30 per region, with per-region totals that are distinct *if computed
# correctly* -- so the bug is unmissable when all three come out equal.
_ORDERS = [
    {"region": REGIONS[i % 3], "amount": float(10 + (i % 7) * 5 + (i % 3) * 100)} for i in range(90)
]


def build_report(orders: list[dict[str, object]]) -> dict[str, dict[str, float]]:
    """Aggregate orders into per-region totals. Correct-looking, quietly wrong.

    Complexity: O(orders). The defect is O(1) and structural -- one shared dict.
    """
    # BUG: one dict, aliased under all three keys. Per-key `{...}` would fix it.
    # (ruff RUF024 flags exactly this footgun -- silenced because it IS the demo.)
    totals = dict.fromkeys(REGIONS, {"sales": 0.0, "orders": 0})  # noqa: RUF024
    for order in orders:
        bucket = totals[order["region"]]  # type: ignore[index]
        bucket["sales"] += order["amount"]  # type: ignore[operator]
        bucket["orders"] += 1
    return totals  # type: ignore[return-value]


def main() -> dict[str, dict[str, float]]:
    report = build_report(_ORDERS)
    for region in REGIONS:
        # Every region prints the grand total -- the visible symptom.
        print(f"{region:>6}: ${report[region]['sales']:.2f} ({report[region]['orders']} orders)")
    return report


if __name__ == "__main__":
    main()
