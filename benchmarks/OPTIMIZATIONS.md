# Optimization log (day 41)

Phase 6, working top-down from `PROFILE.md`, one bottleneck at a time. Every entry has a
hypothesis, an expected win stated *before* measuring, the actual number, and a **kept/reverted**
verdict. Reverted experiments are here on purpose: "I tried X, it won 2%, cost readability, so I
reverted it" is a stronger signal than a list of only wins.

**Method.** The iteration measure is `profile_recorder.py micro` (capture/digest ns per value
shape, median of 5 batches), taken **controlled back-to-back** — measure the change, `git stash`
it, measure the baseline, `git stash pop` — so machine-state drift cannot masquerade as a win.
The correctness gate after *every* change is the full suite, including the replay-equivalence
**referee** and the Hypothesis property campaign. The raw ns are noisy at the µs scale on a loaded
laptop; the **relative** back-to-back delta is the trustworthy figure.

---

## #1 — Fast-path leaf atoms in `capture()` · KEPT

**Bottleneck:** value capture traversal, ~55% of recorder self-time (PROFILE.md #1).

**Hypothesis:** most captured *nodes* are leaf atoms (`int`, `str`, `bool`, `float`, `None`) —
every value inside every container. Each one otherwise pays a `_handler_for` dict probe **plus** a
handler function call to `_atom`/`_string` that only returns the value. An inline exact-type check
at the top of `_capture` that returns the atom directly skips both.

**Expected (before measuring):** ~10–20%. Atoms are the majority of nodes but each is already
cheap; the win is two skipped function calls per atom, not a new algorithm.

**Actual (controlled back-to-back micro):**

| value shape | capture before | capture after | delta |
|---|---:|---:|---:|
| `int` | 518 ns | 453 ns | **−13%** |
| 4-key dict | 3 359 ns | 2 989 ns | **−11%** |
| 200-record list | 284 377 ns | 238 525 ns | **−16%** |

`digest` unchanged (~152k ns on the list), confirming the change is isolated to capture. End to
end: capture is ~55% of recorder overhead, so a ~13% capture win is ~7% off the recorder's total.

**Correctness:** the checks are exact-type `is` (never `isinstance`), so a subclass — `class
MyInt(int)`, an `IntEnum`, a `str` subclass — *misses* the fast path and falls through to the
registry exactly as before; the output is byte-for-byte identical. Referee + property campaign +
full suite green. This is precisely the change the referee exists to make safe.

**Verdict: KEPT.** In profile order, un-clever, one number.

---

## #2 — Content digest (`repr` + blake2b, ~25%) · INVESTIGATED, DEFERRED

**Bottleneck:** the digest is the single hottest leaf (PROFILE.md #2). Two Python-level ideas were
weighed and **neither is a safe cheap win**, so nothing shipped:

- *Cheaper hash.* `blake2b(digest_size=16)` is already among the fastest stdlib hashes; `md5`/`sha1`
  are slower, and the only faster option (`hash()`) is 64-bit and per-process randomised — halving
  the collision margin and breaking cross-run stability. A weaker content hash risks a wrong dedup,
  i.e. silent recording corruption. Rejected.
- *Dedup immutables by identity before serialising* (the day-8 shortcut applied one step earlier).
  Rejected: it re-introduces exactly the `id()`-reuse trap content-addressing was chosen to avoid
  (ADR-0003), and a rare id-reuse collision is timing-dependent — the referee might not reproduce
  it, which is precisely the class of bug that must never ship.

The real win is **fusing capture + structural hash into one pass** (no intermediate `repr` string),
which is the large value-capture change ADR-0013 already scoped as the Rust/rewrite candidate — out
of scope for a measured-Python-tweak day. **Deferred, with reasoning, not hacked.**

---

## #3 — Compile redaction globs to one regex · KEPT

**Bottleneck:** redaction `fnmatch`, ~30% of `tight_loop` (PROFILE.md #3). Note the realistic
`json_pipeline` puts it at ~2% — but `tight_loop` is the hostile reader's quoted number, the fix is
low-risk, and it helps both. `should_redact` ran `name.lower()` then **one `fnmatchcase` per
pattern** (8 by default), per local, per line.

**Hypothesis:** the eight globs can compile to a **single** regex (`fnmatch.translate` joined by
`|`), turning eight Python-level fnmatch calls into one C-level `match`.

**Expected (before measuring):** ~3-5× on `should_redact` — eight calls to one.

**Actual (controlled back-to-back):** **1866 → 419 ns/name, −78% (4.5×).**

**Correctness:** `fnmatch.translate` is what `fnmatchcase` uses internally, so semantics are
identical for *any* glob, including a custom pattern with `?`/`[...]`; an empty pattern set compiles
to `(?!)` (never matches → redact nothing), preserving the old `any([])==False`. The redact/scope
suite + property campaign + full suite green — and redaction is a **security** path, so "green"
here means the secret-name coverage still fires.

**Verdict: KEPT.** Also corrects a now-false docstring claim ("negligible against capture cost").

---

## #4 — `frozenset` membership vs the `is`-chain for the atom fast path · REVERTED

**Hypothesis:** the five-way `is`-chain in #1 could be a single `frozenset` membership
(`type(obj) in _ATOM_TYPES`), which would also fold `None` in uniformly and read cleaner.

**Expected (before measuring):** faster or at least a wash-and-cleaner.

**Actual (controlled back-to-back):**

| | `is`-chain (kept) | `frozenset` (tried) |
|---|---:|---:|
| `int` | **400 ns** | 416 ns |
| 4-key dict | 2 596 ns | **2 441 ns** |

A **wash**, direction-ambiguous, both deltas within run-to-run noise. The `is`-chain is *faster*
for `int` — the single most common capture — because `type(obj) is int` short-circuits on the first
comparison, which a set hash-probe cannot beat; the dict edged the other way but inside the noise
floor.

**Verdict: REVERTED.** Churning committed hot-path code for a noise-level, direction-ambiguous
result is exactly the junior 2% grind the stopping rule exists to prevent. `git checkout` restored
the `is`-chain; the referee re-confirmed it. Recording this is the point: a measured non-win is a
stronger signal than pretending every idea landed.

---

## Where I stopped, and why

**Stopped after #1 (capture fast path, kept), #2 (digest, deferred), #3 (redaction, kept), and the
#4 experiment (reverted).** The stopping rule, set in advance: *stop when the next change costs more
complexity or risk than its measured win justifies.* The remaining bottlenecks all fail that test:

- **Digest (~25%)** — the only real win is fusing capture + structural hash, a large value-capture
  change already scoped in ADR-0013 as the Rust/rewrite candidate. Not a measured-Python-tweak.
- **`ObjectIdentity.of` (~7-13%)** — the win is making identity *lazy* (assign ids only when a
  recording is opened in the UI). That is a behaviour change tied to issue #9, not a hot-path
  micro-opt; it belongs with the UI/identity work, not here.
- **`_handler_for` dispatch (~5%)** — already the exact-type dict the day-7 benchmark chose (113 ns
  vs 202 ns isinstance). There is no cheaper correct dispatch to reach for.

Everything left is either a large change with real risk or a sub-5% grind that would cost
readability. Two clean wins, one honest deferral, one honest revert, a regression guard, and a
written reason to stop — that is a finished optimisation day. The rest is Phase-6-later (ADR-0013),
gated on re-profiling after these landed.
