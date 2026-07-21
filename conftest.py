"""Hypothesis profiles: how hard the campaign runs, and where.

One knob, three settings, and the tradeoff is the whole point. Property testing finds
bugs in proportion to examples tried, but a PR that takes twenty minutes to report a
failure stops being read. So the depth lives on a different clock from the feedback:

* **ci** -- a few dozen examples on every push. Enough to catch a regression that breaks
  most programs, fast enough that nobody learns to ignore it.
* **dev** -- the default when you run `pytest` locally.
* **nightly** -- thousands of examples, once a day, where a bug that appears in one
  program in a thousand has time to surface. Nobody is waiting on it, so it can afford to
  be thorough.

Select with `pytest --hypothesis-profile=nightly`.

Why this file sits at the repo root
-----------------------------------
It belongs next to `tests/property/`, and cannot live there. Hypothesis's pytest plugin
resolves `--hypothesis-profile` at configure time, before a subdirectory's `conftest.py`
is imported -- so registering the profiles under `tests/property/` worked when that path
was named on the command line and made a whole-suite `pytest` die with
`Profile 'dev' is not registered`. The root `conftest.py` is loaded early enough for both.
"""

from __future__ import annotations

from hypothesis import HealthCheck, Verbosity, settings

# Every example records a program and reconstructs it hundreds of times, so the
# per-example cost is milliseconds rather than microseconds. Hypothesis's default deadline
# and slowness checks are calibrated for pure functions and would fail these on timing
# alone -- which would be a false failure about the clock, not about correctness.
_SLOW = [HealthCheck.too_slow, HealthCheck.data_too_large]

settings.register_profile("ci", max_examples=40, deadline=None, suppress_health_check=_SLOW)
settings.register_profile("dev", max_examples=100, deadline=None, suppress_health_check=_SLOW)
settings.register_profile(
    "nightly",
    max_examples=3000,
    deadline=None,
    suppress_health_check=_SLOW,
    verbosity=Verbosity.normal,
)
