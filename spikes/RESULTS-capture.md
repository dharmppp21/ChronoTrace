# Spike results: value capture

**Question:** can we capture values cheaply and safely?

**Answer: safely, yes — provably. Cheaply, only with change detection, and the
sound version of that is an open problem.** The naive combined figure is 2,370×
and would have killed the project. This spike found that before ADR-0001 bet on it.

---

## Environment

Same machine as `RESULTS-overhead.md`: i5-13450HX, Windows 11 build 26200,
Python 3.14.3, High performance power scheme. Reproduce:

```bash
pip install -e ".[dev]" && pip install msgpack
python spikes/spike_capture.py          # per-type cost + serialisation
python spikes/bench_overhead.py --reps 5   # combined overhead
```

---

## 1. Why not stdlib — `reprlib` was measured, then rejected

`reprlib.Repr` is a near-perfect fit on paper: `maxlevel`, `maxlist`, `maxdict`,
`maxstring` are exactly our policy, and it handles cycles, depth-10k and 10M-element
lists in **microseconds**. It was rejected on two grounds, both demonstrated:

| Requirement | reprlib | verdict |
|---|---|---|
| Bounded (depth/size/string) | ✅ 0.01–0.06 ms | fine |
| Handles cycles | ✅ | fine |
| **Never invokes user code** | ❌ **ran `ReprExplodes.__repr__`** | **fatal** |
| Structured output | ❌ returns `str` | **fatal** |

The sentinel fired: `reprlib` called the user's `__repr__`. It *swallows* the
exception and returns `<ReprExplodes instance at 0x...>`, but the code already
ran. For a debugger that is a **correctness** bug, not a performance one — a
`__repr__` that mutates state means observing changed the observed.

Second, it returns a string: `"{'a': [1, {'b': (...)}]}"`. A string cannot be
expanded on click, diffed against the previous instant, or carry an identity badge.

**We borrowed its policy shape and wrote our own traversal.** `test_reprlib_does_invoke_user_code`
pins the rejection — if a future Python makes reprlib safe, that test fails and we
should delete our code and use it. A rejection with no expiry check is dogma.

## 2. Capture cost per type

**Typical locals — the 99% case, where users feel the cost:**

| value | µs/capture | json bytes |
|---|---:|---:|
| int / float / bool / None | 0.12–0.22 | 2–7 |
| str (short) | 0.33 | 13 |
| small_list (5) | 2.29 | 69 |
| small_dict (3 keys) | 2.86 | 102 |
| tuple | 1.57 | 70 |
| **list_of_dicts (20)** | **40.48** | 1,620 |

**Hostile zoo — all bounded, none raised, none ran user code:**

| value | µs | bytes | note |
|---|---:|---:|---|
| self_referential_list | 4.44 | 100 | cycle marker |
| mutual_pair | 17.27 | 243 | `a.peer.peer is a` |
| **huge_list (10M)** | **16.68** | 450 | see the islice bug below |
| deep_dict (10k deep) | 8.12 | 426 | depth marker at 6 |
| repr_explodes | 5.13 | 91 | **user code NOT run** |
| property_side_effects | 4.39 | 111 | **user code NOT run** |
| fabricates_attributes | 4.07 | 99 | `__getattr__` never fired |
| socket / lock / file / generator | 3.4–8.1 | 86–136 | opaque summary, never the resource |
| fake_array (4GB buffer) | 1.16 | 154 | shape/dtype only |
| slotted | 4.75 | 104 | slot descriptors read directly |
| long_string (5M chars) | 0.57 | 571 | truncated + marked |

### The bug the measurement caught

First run: **`huge_list` took 70,744 µs.** The cause was mine:

```python
items = [capture(v, ...) for v in list(obj)[:max_items]]
#                                 ^^^^^^^^^ materialise 10M elements, then take 100
```

The policy said "capture at most 100 items"; the code said "copy ten million
things, then take 100". `itertools.islice(obj, max_items)` is O(max_items):

**70,744 µs → 16.68 µs. 4,240×, from one stdlib function.**

This is why the zoo exists. A capturer that looks correct on `[1,2,3]` is O(n) on
a real data pipeline, and no unit test would have noticed.

## 3. The three invariants — all proven by test

1. **Never invokes user code.** `test_capture_never_invokes_user_code` runs the
   entire zoo and asserts both sentinels stay `False`. Structural, not defensive:
   attributes are read from `obj.__dict__`, and properties/`__getattr__` live on
   the **type**, so an instance dict cannot reach them. `getattr` would fire them;
   we never call it. `__slots__` classes have no instance dict, so the slot
   descriptor is invoked off the type — `getattr` would work until an unset slot
   raised `AttributeError` and fired `__getattr__`, i.e. user code via the error path.
2. **Bounded.** Every hostile case captures to **under 4 KB**. A capture that
   "succeeds" by emitting 4 GB has not succeeded.
3. **Never retains.** `test_capture_does_not_retain_the_object`: capture, drop the
   last strong ref, `gc.collect()`, assert the weakref is dead. Retaining would
   change when the program's finalisers run — the debugger altering the timing of
   the thing under observation.

## 4. The `id()` trap — reproduced, not theorised

`id()` is unique only among **live** objects. CPython reuses addresses.
`test_id_is_reused_after_gc` allocates and frees in a loop and **finds a collision
within 10,000 iterations.**

Why it matters: if `id()` were the durable identity in a recording, two different
objects minutes apart would share one identity, and the UI would draw an "is the
same object" badge between things that never coexisted. That badge is exactly how
a user spots an aliasing bug — so the feature would actively mislead on the bug
class it exists to catch.

**The fix**, proven by `test_weak_identity_survives_id_reuse`: a monotonic counter
handing out ids on first sight, stored in a `WeakKeyDictionary`. Weak so the
recorder never extends a recorded object's lifetime. Raw ids collide; assigned ids
do not.

**Note the distinction.** Inside a *single* capture, keying the cycle-detection set
on `id()` is correct — every object on the path is alive, held by our own frames.
The trap is about *durable* identity across a recording. Two different problems;
conflating them would cost either correctness or speed.

## 5. Serialisation

Captured representations of typical locals:

| format | total bytes | µs/value | verdict |
|---|---:|---:|---|
| **msgpack** | **1,004** | 5.29 | **chosen** — 47% smaller than json |
| json | 1,891 | 3.58 | fallback; human-readable, bigger |
| pickle | 1,130 | 4.16 | **REJECTED — security** |

### pickle is rejected on security, not performance

An **163-byte** malicious "recording":

```
marker exists before load : False
pickle.loads(payload)          # <- merely OPENING the recording
marker exists after load  : True
marker contents           : a recording just wrote this file
```

`pickle.loads` executes `__reduce__`. Opening a `.chrono` file must **never** be
able to run code, because the whole workflow is *people share recordings of
crashes in bug reports*. Opening a stranger's recording is the normal path, not an
edge case.

msgpack and json cannot even express the attack — both raise `TypeError` on the
malicious object. This is a **spec-level** ban for day 11, not an implementation
preference.

## 6. Recommended capture policy

| knob | default | justification |
|---|---:|---|
| `max_depth` | 6 | depth-10k dict → 426 bytes, 8 µs. Also bounds recursion, so an explicit work-stack is unnecessary. |
| `max_items` | 100 | 10M list → 450 bytes, 16 µs (with islice). |
| `max_string` | 512 | 5M-char string → 571 bytes, 0.57 µs. |

**Capture is lossy by policy, and the loss must be visible.** Every truncation
carries `truncated: True` and the real `len`. `test_truncation_is_visible_not_silent`
asserts `capture(huge_list)` reports `len=10000000, items=100, truncated=True`.
A user shown 100 of 10,000,000 items with no marker will believe the list has 100
items and debug the wrong thing. Silent truncation is the difference between a
lossy tool and a lying one.

**Representation is plain nested dict/list/atoms — not a class hierarchy.** It is
directly msgpack/json-serialisable with no encoder, and a class would be an
abstraction over data that is already the right shape. Atoms pass through
unwrapped; every container is wrapped in a tagged dict, so a user dict containing
a `"$"` key is never confused with our tag.

---

## 7. The combined figure — the number that matters

Day 2 measured a callback that appended a tuple. That is not recording. This is:

| workload | baseline | scoped (day 2) | **+ naive capture** | **+ change detection** |
|---|---:|---:|---:|---:|
| tight_loop | 8.29 ms | 16.4× | **123.3×** | 107.3× |
| fib_recursive | 5.43 ms | 10.0× | **35.7×** | 32.9× |
| **json_pipeline** | 7.08 ms | 1.3× | **2,370.5×** | **6.1×** |
| io_bound | 159.37 ms | 1.0× | 1.0× | 1.0× |

### Naive capture is a catastrophe: 2,370×

A 6.88 ms program takes **16.5 seconds**. Capturing every local on every line
re-walks `records` (1200 dicts) on all 13,210 lines, though it never changes after
line one — ~1.3M redundant item captures.

Day 2's cheerful "1.5×, tolerable with room to spare" was measuring the floor.

### Change detection rescues it: 6.1× — a 387× improvement

Skipping bindings whose value identity is unchanged takes json_pipeline from
2,370× to **6.1×**. This is not an optimisation. It is the difference between the
architecture working and not working.

Note it barely helps `tight_loop` (123× → 107×): there, `i`, `x`, `y` and `total`
all change every iteration, so there is nothing to skip.

### The open problem this exposes — flagged, not buried

`mon_capture_changed` uses **identity as a proxy for "unchanged", which is
unsound**. `lst.append(x)` mutates in place and keeps the same `id`, so this
design would miss the write and show the user **stale state** — the worst failure
a debugger can have.

Day 8's planned rule (identity shortcut for immutable types only, re-capture
mutables) is sound but **does not obviously save us here**: `records` and `parsed`
are lists, i.e. mutable, so they would be re-captured on every line and the cost
climbs back toward the catastrophe. `max_items=100` bounds each re-capture to
~40 µs, which across 13,210 lines is still ~530 ms ≈ 75×.

**So 6.1× is a ceiling, not a promise.** The honest figure sits between 6.1× and
2,370×, and where it lands is day 7–8's central problem. Candidate directions:

- **Don't capture all locals every line.** Capture on binding *writes*, not on
  every LINE event. Requires cheaply knowing what changed.
- **Shallow capture + structural sharing.** Capture a container as child *refs*,
  so re-capturing an unchanged 1200-element list costs 1200 id lookups rather than
  1200 deep captures.
- **Content hashing with a dedup cache** (day 8's plan) — sound, but you pay the
  capture before you learn it was a duplicate.

---

## Verdict

**Capture is safe: proven.** Never runs user code, never retains, always bounded,
survives the entire hostile zoo — every claim backed by a passing test rather than
an assertion.

**Capture is affordable: conditionally.** ~6× on realistic code *if* change
detection works soundly; ~107× on tight numeric loops even then. Compare to `pdb`
at 50–100×, which people use daily. Tolerable, with `--sample` (day 42) as the
escape hatch for the tight-loop case.

**The condition is load-bearing and unresolved.** Omniscient recording lives or
dies on sound change detection. ADR-0001 must record that as the primary risk, not
a footnote.

## Open questions

- What is the sound-change-detection cost, between 6.1× and 2,370×? (Days 7–8.)
- Does `set_local_events` on discovered code objects beat global `set_events` +
  `DISABLE`? (Carried from day 2.)
- `f_locals` in the callback uses `sys._getframe(1)`; day 5 must decide whether the
  recorder maintains its own frame model instead. PEP 667 made `f_locals` a
  write-through proxy in 3.13+; its cost is inside the numbers above but was not
  isolated.
