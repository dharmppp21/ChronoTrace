# ChronoTrace

**A time-travel debugger for Python.** Record your program once, then scrub backward through its execution to find the bug.

> **Status: in development — nothing works yet.**
> This repository is at Day 1 of a 50-day build. There is no recorder, no storage
> format and no UI: today's commit is the packaging, linting, typing and CI
> baseline. Everything below the "Planned" heading is a plan, not a claim.
> Follow the [ADR log](docs/adr/) to watch the decisions get made.

## Why

Debugging only goes forward. Breakpoints show you the program *now*, so the
moment you realise the bug happened 200 steps ago, you restart and guess where
to break — over and over. The information you needed was computed once and then
thrown away.

ChronoTrace keeps it.

## Planned

Each of these is a phase of the build, not a shipped feature:

| Capability | What it means | Phase |
|---|---|---|
| Recording | Capture every line, call, return, exception and local value via PEP 669 `sys.monitoring` | 1 |
| `.chrono` format | Append-only, CRC-framed, zstd-compressed columnar log | 2 |
| Backward stepping | Step *back* through execution; reach any past instant in bounded time | 3 |
| Causal queries | "Who last wrote to `total`?", "Where did this exception originate?" | 4 |
| Timeline UI | Drag a playhead over the whole execution and watch variables change backward | 5 |

## How it will work

State is stored the way a video codec stores frames: **full keyframes every N
events, deltas in between**. Reaching any past instant is then a binary search to
the nearest keyframe plus a bounded number of deltas — which is what makes
scrubbing feel instant instead of requiring a re-run.

```
 target.py ──▶ recorder ──▶ store ──▶ index ──▶ query ──┐
             (sys.monitoring) (mmap+zstd) (sqlite)       ├──▶ server ──▶ UI
                                  └──▶ reconstruct ──────┘
                                    (keyframe + deltas)
```

Dependencies point one way only: `server → query → reconstruct → index → store → recorder`.

## Requirements

- Python 3.12+ (3.14 recommended — PEP 669 `sys.monitoring` is the recording mechanism)
- Linux, macOS or Windows

## Development

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows
pip install -e ".[dev]"

ruff check . && ruff format --check .
mypy
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the standards this project holds itself to,
and [docs/adr/](docs/adr/) for why it is built the way it is.

## A note on recordings

A `.chrono` recording contains the full memory of the program that produced it —
including any credentials, tokens or personal data that program held. **Treat a
recording as you would a core dump.** They are gitignored by default. A threat
model lands in Phase 7.

## License

MIT — see [LICENSE](LICENSE).
