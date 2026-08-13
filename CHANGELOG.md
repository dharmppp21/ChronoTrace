# Changelog

All notable changes to ChronoTrace are documented here, in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format, following
[semantic versioning](https://semver.org/spec/v2.0.0.html).

**Why this file is written by hand rather than generated from commits.** The commit log is
written for the people changing the code: it says `perf(cli): lazy subcommand imports for
sub-100ms --help`, which is the right message for a reviewer and the wrong one for a user. What
a user needs from that same change is "the CLI starts faster" — or, more often, nothing at all,
because it changed no behaviour they can see. Generating notes from commits produces a list
that is complete, accurate, and unreadable; the translation from *what changed in the code* to
*what changed for you* is the entire value of a changelog, and it is not mechanical. Release
notes on GitHub are pulled from the matching section below (see `.github/workflows/release.yml`),
so this file is the single source for both.

Entries are grouped as Added / Changed / Deprecated / Removed / Fixed / Security.

## [Unreleased]

Nothing yet.

## [0.1.1] - 2026-08-14

### Added

- **Queries no longer need the recording's path.** `chronotrace query --watch total` uses the
  newest `.chrono` in the current directory — the one you just recorded — and prints which it
  chose, so the shortcut is never silent. Name a file explicitly when you mean a different one.

### Fixed

- **`chronotrace record --ui` opens the scrubber.** It was opening `/docs`, the raw API
  endpoint list, so the one-command "record it and watch in the browser" path quietly delivered
  the wrong page. `/docs` remains the fallback for an install without the bundled UI.

## [0.1.0] - 2026-08-13

The first public release: a complete, installable time-travel debugger. Everything below was
built over the preceding phases; this is the first time any of it is available to someone who
is not building it from source.

### Added

- **Record a Python program's execution, then step backward through it.** `chronotrace record
  your_script.py` writes a `.chrono` recording; `chronotrace step` opens it and moves in both
  directions — previous line, previous call, out of the current frame — over a program that has
  already finished running.
- **Retroactive breakpoints.** Set a breakpoint *after* the program has run and ask where it
  would have hit: `chronotrace query run.chrono --break app.py:42`. Because the run is already
  recorded, there is nothing to reproduce and nothing to catch a second time.
- **Queries over a finished run**, including every write to a variable (`--var-writes`), every
  change of a value (`--watch`, with `--changed-to` / `--changed-from` filters), and line hit
  counts. `chronotrace query --list` shows them all.
- **A browser timeline.** `chronotrace serve` opens a scrubber over a directory of recordings:
  drag through execution and watch source, variables, the call tree and query results follow.
  Install it with `pip install chronotrace[ui]`.
- **Live recording.** `chronotrace record --ui script.py` streams events to the browser while
  the program is still running, so a long run can be watched rather than waited out.
- **`chronotrace --version`** reports the package version, the `.chrono` format version, and
  whether the `[ui]` extra is installed — the three things any bug report needs first.
- **`chronotrace repair`** recovers a recording from a process that was killed mid-write.

### Changed

- **Recording is dependency-light on purpose.** The recorder is imported into *your* process, so
  every dependency it has becomes a dependency of your program. The core install carries only
  `zstandard` and `msgpack`; the web server lives behind `chronotrace[ui]`, and asking for
  `chronotrace serve` without it gives an actionable message rather than an `ImportError`.

- **Paths are matched the way your platform matches them.** On Windows the separator and letter
  case you type no longer decide whether a query finds your file, so
  `--break C:/proj/app.py:42` works against a recording that interned `C:\proj\app.py`. POSIX
  paths stay case-sensitive, as they should be.

### Known limitations

Stated here rather than discovered later:

- **Capture mode is slow on hot loops.** Recording control flow alone costs roughly 6× on a
  realistic program; capturing every value costs far more (~1,500× on that same program, and
  worse on a tight numeric loop). `docs`/`benchmarks/RESULTS.md` publish the measurements and
  the methodology, and the README has a "When *not* to use ChronoTrace" section. This is a
  debugging tool for a program you are already stuck on, not something to leave switched on.
- **Not for production.** A recording is your program's memory, secrets included. `serve` binds
  to loopback by default and warns loudly if you change that.
- **Object identity is not tracked for `dict` and `list`** ([#9]), so the UI's aliasing badge
  does not appear for those types.
- **The `.chrono` metadata block is unused**, which is what currently blocks sampled recording
  ([#18]).

[#9]: https://github.com/dharmppp21/ChronoTrace/issues/9
[#18]: https://github.com/dharmppp21/ChronoTrace/issues/18
[Unreleased]: https://github.com/dharmppp21/ChronoTrace/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/dharmppp21/ChronoTrace/releases/tag/v0.1.1
[0.1.0]: https://github.com/dharmppp21/ChronoTrace/releases/tag/v0.1.0

<!-- 0.1.0rc1 and 0.1.0rc2 are deliberately absent above. They were release-candidate
     rehearsals published to TestPyPI only, never to PyPI, and their contents are folded into
     0.1.0. A changelog is for people using the package; versions they cannot install are noise
     to them, and the git tags remain for anyone who wants the history. -->
