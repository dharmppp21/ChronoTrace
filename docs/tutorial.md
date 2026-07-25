# Tutorial: debugging without prints or re-runs

This walks through three real bugs, each found with **queries alone** — no `print`, no
stepping, no re-running the program, no reading the source first. It is written from an
actual session (day 31's dogfooding), so the friction and the wins are both real.

The loop every time is the same: **record once, then ask.**

```bash
chronotrace record buggy.py          # writes buggy.chrono and builds its index
chronotrace query buggy.chrono ...   # ask as many questions as you like, no re-running
```

Every result line begins with `[seq]` — an instant you can jump to. That is the whole idea:
a query returns *places in time*, not text.

---

## Bug 1 — a number that is quietly wrong

`examples/mystery/off_by_one.py` computes an average and prints `15.0`. You expected `25.0`.
There is no crash and no traceback — the worst kind of bug. Start where the wrong value is:
the accumulator.

```bash
$ chronotrace query off_by_one.chrono --watch total
[25]  = unset -> 0
[29]  = 0 -> 10
[33]  = 10 -> 30
[37]  = 30 -> 60
```

`total` stops at **60**. It added 10, 20, 30 — and then stopped, never adding the fourth
value. So the loop ran one time short. Confirm by watching the loop variable:

```bash
$ chronotrace query off_by_one.chrono --var-writes i
[27]  off_by_one.py  average  = 0
[31]  off_by_one.py  average  = 1
[35]  off_by_one.py  average  = 2
```

`i` takes **0, 1, 2** and never reaches 3. The last element is never summed — a classic
off-by-one in the loop bound. **Two queries, no stepping.**

---

## Bug 2 — state that leaks between calls

`examples/mystery/sticky_default.py` calls `collect("first")` then `collect("second")` and
returns `['first', 'second']`. The second call was supposed to return just `['second']`.
Watch the bucket the function fills:

```bash
$ chronotrace query sticky_default.chrono --watch bucket
[24]  = unset -> []
[26]  = [] -> ['first']
[33]  = ['first'] -> ['first']
[35]  = ['first'] -> ['first', 'second']
```

Read seq **33**: the *second* call's `bucket` opens as `['first']`, not `[]`. The two calls
share one list — the mutable default argument, evaluated once and reused. **One query.**

---

## Bug 3 — a closure that captured the wrong thing

`examples/mystery/late_binding.py` builds multiplier functions for factors 2, 3, 4, takes
the second (expecting "multiply by 3"), applies it to 10, and gets **40** instead of 30.
Ask what `factor` was, everywhere it was set:

```bash
$ chronotrace query late_binding.chrono --var-writes factor
[28]  late_binding.py  make_multipliers            = 2
[32]  late_binding.py  make_multipliers            = 3
[36]  late_binding.py  make_multipliers            = 4
[46]  late_binding.py  make_multipliers.<locals>.<lambda>  = 4
```

The last line is the key: when the lambda finally runs (seq 46), it reads `factor = 4` —
the loop's *final* value. All three closures share one `factor` cell, so every one of them
multiplies by 4. **One query.**

---

## When to reach for which query

| you want to know | query |
|---|---|
| how a value evolved | `--watch NAME` |
| every time a variable was set | `--var-writes NAME` |
| where a value came from | `--provenance NAME@SEQ` |
| every time a line ran (a retroactive breakpoint) | `--break FILE:LINE` |
| ...only when a condition held | `--break FILE:LINE --if "i > 100"` |
| where an exception was born | `--exception-origin SEQ` |
| who called a function | `--callers-of NAME` |

The full reference, with complexity and latency for each, is [queries.md](queries.md).

## The honest part

These three bugs are all *state* bugs, and state is exactly what ChronoTrace recorded — so
it wins cleanly. It does **not** record thread interleavings beyond the events it saw, the
internals of C extensions, or timing-dependent races, and it cannot attach to a
already-running process the way `pdb` can. The [README](../README.md#chronotrace-vs-pdb)
has the full comparison, including where `pdb` is the better tool.
