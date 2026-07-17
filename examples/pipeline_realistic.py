"""A realistic, stdlib-heavy pipeline: the shape of code people actually debug.

Correct (unlike buggy_pipeline) and deliberately routed through pure-Python stdlib
-- `json`, `datetime.strptime`, `statistics`, `collections.Counter` -- so the
recorder meets real library calls, not just a hand-written loop. This is the
integration test's proof that scope filtering keeps the stdlib out of the timeline
(days 9): the recording should contain this file's lines and none of `_strptime`'s.

Parse -> normalise timestamps -> aggregate by region -> summarise. ~50 records,
small enough to record in a blink, real enough that the event stream looks like a
program and not a microbenchmark.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime
from typing import Any

_RAW = json.dumps(
    [
        {
            "id": i,
            "ts": f"2026-07-{(i % 28) + 1:02d} {(i % 24):02d}:{(i % 60):02d}:00",
            "region": ["emea", "apac", "amer"][i % 3],
            "amount": (i * 37) % 500,
        }
        for i in range(50)
    ]
)


def normalise(raw: str) -> list[dict[str, Any]]:
    """Parse JSON and pull the hour out of each timestamp."""
    out: list[dict[str, Any]] = []
    for record in json.loads(raw):
        when = datetime.strptime(record["ts"], "%Y-%m-%d %H:%M:%S")
        out.append(
            {
                "id": record["id"],
                "hour": when.hour,
                "region": record["region"],
                "amount": record["amount"],
            }
        )
    return out


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals per region, plus the mean order size."""
    by_region: Counter[str] = Counter()
    for record in records:
        by_region[record["region"]] += record["amount"]
    amounts = [record["amount"] for record in records]
    return {
        "by_region": dict(by_region),
        "mean_amount": statistics.mean(amounts),
        "count": len(records),
    }


def main() -> dict[str, Any]:
    report = summarise(normalise(_RAW))
    print(f"regions={report['by_region']} mean={report['mean_amount']:.1f} n={report['count']}")
    return report


if __name__ == "__main__":
    main()
