# Contributing to ChronoTrace

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
source .venv/bin/activate       # Linux / macOS
pip install -e ".[dev]"
```

The `-e` is not optional. This project uses a **`src/` layout**, so `chronotrace`
is not importable from the repo root — you must install it. That is deliberate:
it means the tests exercise the *installed package*, not a directory that happens
to be on `sys.path`. A packaging mistake fails here instead of in a user's
`pip install`.

## The four gates

Everything below must pass before a commit. CI runs all four on
Python 3.12/3.13/3.14 × Ubuntu/macOS/Windows.

```bash
ruff check .            # lint
ruff format --check .   # format
mypy                    # types, --strict
pytest                  # tests
```

## The referee

`tests/equivalence/` is the project's referee: it asserts that the state ChronoTrace
reconstructs is the state the program **actually had**, observed live by a mechanism that
shares no code with the recorder. It is a required check on all nine CI cells.

**A red harness blocks merge. No exceptions.**

Not "investigate before merging" — blocks. Every other test in this repo checks
ChronoTrace against ChronoTrace, so they can all pass while the debugger lies about a
program. This one cannot. If it goes red, one of these is true and each is a stop:

1. Reconstruction is wrong. Fix it.
2. The program diverges for a reason the system genuinely never claimed. Then it belongs
   in `KNOWN_DIVERGENCES` with an open issue and an exact expectation that fails once the
   divergence stops — **never** as a new tolerance in `compare.py`. A lenient comparator
   is a test that always passes and protects nothing.
3. The harness itself is broken (an `AlignmentError` says so explicitly). Fix the harness.

Weakening the comparator to make it green is the one change that will always be rejected
in review. Read [`tests/equivalence/README.md`](tests/equivalence/README.md) before
touching it.

## Standards

These are enforced by review, not just by tooling.

1. **Explain why a file exists.** Every module docstring answers *why this file
   exists* and *what it must never import*. If you cannot say it in one
   sentence, the file probably should not exist.
2. **Comments explain *why*, never *what*.** The code already says what it does.
   No comments addressed to a reviewer.
3. **One-way dependencies.** `server → query → reconstruct → index → store → recorder`.
   Never import upward. Never leak a lower layer's types through a higher layer's
   public API. From day 10 this is an automated import-graph test — a rule nobody
   enforces is a preference.
4. **Earn your abstractions.** No interface with one implementation. No factory
   for one product. No config option for a value nobody sets. An abstraction is
   justified by a second *real* caller, never a hypothetical one.
5. **No duplicated logic.** Search before writing a helper.
6. **Size limits.** Functions under ~40 lines, files under ~400. One reason to
   change per module.
7. **State complexity** in the docstring of anything non-trivial. It is a
   promise, not a note.
8. **Readability first.** Performance work happens only where a profile says so.
   The recorder hot path is the sole exception, and only with a number in the
   commit message.
9. **Tests ship with the change**, not after. Any branch, loop, parser or trust
   boundary gets one.
10. **No TODOs.** A deliberate simplification gets a tracked issue with a repro,
    not a comment that rots.

## Decisions

Anything expensive to reverse gets an [ADR](docs/adr/) **on the day you decide**,
including decisions *not* to build something. See [docs/adr/README.md](docs/adr/README.md).

## Commits

Conventional commits, scoped to one logical change:

```
feat(recorder): bounded cycle-safe value capture that never invokes user code
perf(store): 3x faster index build by creating indexes after bulk load
fix(query): attribute nonlocal writes to the enclosing frame
docs: ADR-0004 chrono file format
```

A day's work is several scoped commits, never one dump. Performance commits carry
a before/after number in the message.

## Security

A `.chrono` recording contains the full memory of the recorded program —
credentials, tokens, personal data. **Never attach one to an issue.** Run
`chronotrace doctor` and paste that instead (available from Phase 7). Recordings
are gitignored; keep it that way.

To report a vulnerability, see `SECURITY.md` (Phase 7).
