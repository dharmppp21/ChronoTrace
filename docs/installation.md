# Installing ChronoTrace

```bash
pipx install chronotrace
```

That is the whole thing. `chronotrace --version` confirms it worked. If you have never
recorded a program before, jump to the [README quickstart](../README.md).

ChronoTrace ships as a pure-Python wheel with the browser UI **prebuilt inside it**, so
installing needs **no C compiler and no Node.js** — only Python 3.12 or newer.

## The recommended way: pipx

```bash
pipx install chronotrace          # the CLI
pipx install "chronotrace[ui]"    # ...plus the browser scrubber (chronotrace serve)
```

[pipx](https://pipx.pypa.io) installs a command-line app into its **own** isolated
environment and puts `chronotrace` on your PATH. That is exactly right for a debugger: it
never has to share a virtualenv with the program you are debugging, so their dependencies
can never collide.

## pip, into a virtual environment

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install chronotrace
```

`pip install --user chronotrace` works too. Prefer a venv or pipx over a bare
`pip install` into the system interpreter.

## The extras, and why the split exists

| Install | You get | Pulls in |
|---|---|---|
| `chronotrace` | record, query, step, repair — the whole CLI | `zstandard`, `msgpack` |
| `chronotrace[ui]` | the above **plus** `chronotrace serve` (the browser scrubber) | `fastapi`, `uvicorn` |
| `chronotrace[all]` | everything a user can enable (today, that is `[ui]`) | — |

This split is a **correctness** decision, not tidiness. **The recorder is imported into
the process you are debugging** — so every dependency ChronoTrace's core takes becomes a
dependency of *your* program too: another entry in its `sys.modules`, another version that
could clash with yours, another package the recorder must exclude from what it records. A
debugger that cannot attach to your project because of a version conflict in *its own*
dependency tree is one nobody keeps installed. So the core stays at two small, pure-data
libraries, and the web framework only arrives when you ask for `[ui]`.

If you run `chronotrace serve` without the extra, you get a one-line, actionable message —
`pip install chronotrace[ui]` — not a stack trace.

## From source

You only need this to develop ChronoTrace or to build an unreleased version. Building the
**browser UI** from source needs **Node.js 20+** (the published wheel does not — it carries
the UI prebuilt).

```bash
git clone https://github.com/dharmppp21/ChronoTrace
cd ChronoTrace
pip install -e ".[dev]"          # the CLI + the test/lint toolchain
python -m build --wheel          # builds the frontend and bundles it into dist/*.whl
```

`python -m build` runs the frontend build hook (`hatch_build.py`): with Node present it runs
`npm ci && npm run build`; without Node it falls back to any prebuilt UI, or produces an
API-only wheel. Set `CHRONOTRACE_SKIP_UI_BUILD=1` to reuse an already-built `_ui/`.

## `chronotrace --version`

```console
$ chronotrace --version
chronotrace 1.0.0
.chrono format 1.7
[ui] extra: installed
```

Three facts, because they are the first three a bug report needs: which package build, which
recording-format version (a `.chrono` from a newer format may not open in an older tool), and
whether the browser UI is available.

## Troubleshooting

- **"No module named …" / `serve` fails** — you installed the core without the UI. Run
  `pip install "chronotrace[ui]"`.
- **pip tries to compile something / "Microsoft Visual C++ required"** — this should not
  happen: `zstandard` and `msgpack` ship prebuilt wheels for every supported platform, and
  ChronoTrace itself is pure Python. If you see it, your Python is one the dependencies have
  no wheel for; upgrade to a supported CPython (3.12–3.14).
- **"chronotrace: command not found"** — the install directory is not on your PATH. With
  pipx, run `pipx ensurepath` and restart the shell. In a venv, activate it first.
- **Behind a corporate proxy / offline** — everything is a normal wheel from PyPI, so the
  usual `pip install --index-url …` / `--proxy …` / pre-downloaded-wheel flows all work; no
  build step means nothing reaches out to npm at install time.
- **Windows** — fully supported and tested on every release; the wheel is platform-neutral.
