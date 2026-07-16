# Configuring ChronoTrace

Everything here is optional. The defaults are chosen so that
`chronotrace run your_script.py` records *your* code — not the standard library,
not your dependencies, not secrets — with no configuration at all.

## Where settings come from

Four layers, each overriding the one below it:

| Precedence | Source | Example |
|-----------:|--------|---------|
| 1 (highest) | CLI flags | `--include '*/requests/*'` |
| 2 | Environment variables | `CHRONOTRACE_INCLUDE=*/requests/*` |
| 3 | `pyproject.toml` | `[tool.chronotrace]` table |
| 4 (lowest) | Built-in defaults | see below |

Most specific wins: a flag on the command line is the most deliberate act, so it
beats an environment variable, which beats the project file, which beats the
defaults. Each layer contributes only the keys it sets; unset keys fall through.

Once recording starts the resolved config is **frozen**. A recording is only
interpretable if every event in it obeyed one set of rules — so the settings
cannot change mid-recording.

## Options

| Option | Default | Meaning |
|--------|---------|---------|
| `roots` | the script's directory | Directories that count as "my code". |
| `include` | *(none)* | Globs that force files **into** scope — how you debug into a dependency. |
| `exclude` | *(none)* | Globs that force files **out of** scope, even under a root. |
| `redact` | `*password*`, `*passwd*`, `*secret*`, `*token*`, `*api_key*`, `*apikey*`, `*auth*`, `*credential*` | Local-variable names whose **values** are withheld. |
| `capture_values` | `true` | Record local values, not just control flow. |

### Scope: why default-narrow

The default records only files under your project root, and **excludes the stdlib
and site-packages even when a virtualenv lives inside the project** (the common
`.venv/` layout). This is faster, produces far smaller recordings, and matches
what "my code" means. Day 9's benchmark shows the effect: scoping is the single
biggest overhead reduction in the project.

Debugging *into* a library is the exception, so it is opt-in:

```bash
chronotrace run app.py --include '*/site-packages/httpx/*'
```

Globs are `fnmatch` patterns matched against the full, forward-slashed path
(write `/` even on Windows). `exclude` beats `include` beats the library
exclusion beats the root inclusion.

`exec`/`eval`-generated code (filename `<string>`) and frozen modules are
**excluded by default** — a timeline pointing at line 4 of source that is not on
disk is worse than silence. Opt in with `--include '<string>'` if you must.

### Redaction: a security feature, with honest limits

Secret-named locals are redacted **before** they are read: on a name match the
value is never passed to the capturer, so it never enters ChronoTrace's buffers,
and a crash dump or partial flush cannot leak it. A redacted local still appears
in the timeline with its name and a `REDACTED` marker — you can tell "hidden"
from "absent".

What redaction **cannot** do, stated plainly:

- **A secret in a variable named `x` is captured.** Detection is by name only.
  Value-based scanning (entropy, key-format regex) is a future opt-in
  ([tracked for day 47](https://github.com/dharmppp21/ChronoTrace/issues)), not a
  default — silent false positives that drop real data are their own failure.
- **A nested secret is captured.** `config["password"]` binds the local `config`;
  redaction sees `config`, not the key, so the whole dict — password included —
  is recorded. Do not rely on name redaction for secrets held inside containers.
- **`*auth*` is broad** and will also match `author`/`oauth`. This is deliberate:
  hiding a non-secret is annoying, leaking a secret is a breach.

Redaction is **on by default**. Narrow or widen it, never silently disable it by
accident:

```toml
[tool.chronotrace]
redact = ["*password*", "*token*", "*secret*", "MY_APP_KEY"]
```

## Examples

`pyproject.toml`:

```toml
[tool.chronotrace]
exclude = ["*/migrations/*"]
capture_values = true
```

Environment (comma-separated lists, `1/true/yes/on` for booleans):

```bash
export CHRONOTRACE_EXCLUDE='*/migrations/*,*/generated/*'
export CHRONOTRACE_CAPTURE_VALUES=true
```

CLI (repeat a flag for multiple globs):

```bash
chronotrace run app.py --exclude '*/migrations/*' --include '*/httpx/*'
```
