"""A tiny cache builder. Each call is supposed to start a fresh bucket. It does not.

Dogfooding (day 31): the bug is not described here. Called twice below; the second call is
expected to return just ["second"].
"""

from __future__ import annotations


def collect(item: str, bucket: list[str] = []) -> list[str]:  # noqa: B006 -- this IS the bug
    bucket.append(item)
    return bucket


def main() -> list[str]:
    collect("first")  # seeds the (buggy) shared default; its result is deliberately ignored
    second = collect("second")
    return second


if __name__ == "__main__":
    print(main())
