# Releasing ChronoTrace

Written for someone who has not done this before — which, in six months, includes you.

Cutting a release is two commands. Everything else in this document is either the one-time
setup that makes those two commands work, or what to do when one of the automated steps fails
partway through.

---

## The two commands

```bash
git tag v0.1.0 && git push origin v0.1.0
```

That is the whole release. `.github/workflows/release.yml` takes it from there.

Before you type it, do the [checklist](#pre-release-checklist).

---

## What happens automatically

The tag triggers five jobs, in this order. The ordering is deliberate: **everything that can
fail runs before the one step that cannot be undone.**

| # | Job | What it does | Reversible? |
|---|-----|--------------|-------------|
| 1 | `guard` | Asserts the tag matches `src/chronotrace/_version.py`, and that the version is not already on the target index | nothing happened yet |
| 2 | `build` | Runs the full test suite, builds the wheel + sdist, `twine check`s them, generates the SBOM | nothing published |
| 3 | `publish` | Uploads to PyPI via Trusted Publishing (OIDC) | **NO — permanent** |
| 4 | `image` | Builds the demo image, smoke-tests it, pushes to GHCR | yes, tags can be overwritten |
| 5 | `github-release` | Creates the GitHub Release with notes from `CHANGELOG.md`, attaching the artifacts and SBOM | yes, can be deleted and recreated |

### PyPI or TestPyPI is decided by the version itself

There is no switch to set and no way to point a real release at the wrong index by accident:

- A **pre-release** version (`0.1.0rc1`, `0.1.0a1`, `0.1.0.dev0`) publishes to **TestPyPI** and
  is marked a GitHub pre-release. The image is pushed under its version tag but **`latest` does
  not move**, so a rehearsal can never change what `docker run ghcr.io/dharmppp21/chronotrace`
  gives a first-time reader.
- A **final** version (`0.1.0`, `1.2.3`) publishes to **PyPI** and moves `latest`.

This is why the rehearsal is a real rehearsal: it runs the identical code path, not a
`if dry_run:` branch that has never been exercised in anger.

---

## One-time setup

You only do this once per index. Neither can be automated — both are account-level actions on
someone else's website, by design.

### 1. Configure Trusted Publishing

Trusted Publishing means there is **no API token in this repository at all**. Instead, GitHub
mints a short-lived OIDC token that proves "this is workflow X in repo Y", and PyPI exchanges
it for upload rights it grants only to that exact identity. Nothing long-lived exists to leak,
rotate, or accidentally print into a log.

**There are two different forms, and which one you need depends on whether the project already
exists on that index.** Getting this wrong is the first thing that goes wrong, because the
obvious page — "publishing" under your *account* — rejects a name that is already taken with a
message that reads like a name collision rather than "you are on the wrong form".

- **Project does not exist yet on that index** → *account* → Publishing → **add a pending
  publisher**. This reserves the name and configures it in one step.
  - PyPI: <https://pypi.org/manage/account/publishing/>
- **Project already exists and you own it** → that *project* → Settings → Publishing → **add a
  publisher**. A pending publisher is not available, and not needed.
  - TestPyPI: <https://test.pypi.org/manage/project/chronotrace/settings/publishing/>
  - `chronotrace` already exists on TestPyPI because it was uploaded there by hand, with a
    token, before this pipeline existed. That is the normal state after any manual dry run.

Either form takes the same values:

| Field | Value |
|-------|-------|
| PyPI project name | `chronotrace` (pending-publisher form only) |
| Owner | `dharmppp21` |
| Repository name | `ChronoTrace` |
| Workflow name | `release.yml` |
| Environment name | `testpypi` (on TestPyPI) / `pypi` (on PyPI) |

The environment name must match the `environment:` in the `publish` job. If OIDC publishing
fails, this table is almost always the reason — the error message names the field that did not
match, so read it rather than guessing.

Once a trusted publisher is configured, the API token used for that manual upload is dead
weight with real blast radius. Delete it on the index, and delete `~/.pypirc` if it holds one —
the whole point of OIDC is that no long-lived credential exists to leak.

### 2. Create the matching GitHub environments

Settings → Environments → New environment, named `pypi` and `testpypi`. Adding **required
reviewers** to `pypi` is worth it: it turns the irreversible step into one that pauses for a
human click, without adding a human step to anything else.

### 3. Branch protection

Settings → Branches → add a rule for `main`:

- Require status checks to pass: `static`, `package (wheel + clean install)`, and at least one
  `test` cell
- Do not allow force pushes

Tag protection (Settings → Tags → `v*`) restricts who can push a release tag. On a solo repo
this guards against your own slip more than anyone else's malice, which is still worth having
given that a tag triggers an irreversible publish.

---

## Pre-release checklist

- [ ] `CHANGELOG.md` has a section for this exact version — the release notes are pulled from
      it verbatim, and a missing section means a release with a placeholder body
- [ ] `src/chronotrace/_version.py` is the version you are about to tag, **without** the `v`
- [ ] `main` is green
- [ ] You have rehearsed this version's *shape* against TestPyPI at least once (see below)
- [ ] The version does not already exist on the target index — `guard` checks this, but knowing
      beforehand saves a failed run

## Rehearsing

Bump to a pre-release, tag it, and watch the whole pipeline run against TestPyPI:

```bash
# in src/chronotrace/_version.py: __version__ = "0.1.0rc1"
git commit -am "chore: 0.1.0rc1" && git push
git tag v0.1.0rc1 && git push origin v0.1.0rc1
```

Then verify what a stranger gets, in a throwaway environment. **Install the published wheel by
URL, not by name:**

```bash
python -m venv /tmp/ct
/tmp/ct/bin/pip install "chronotrace[ui] @ https://test-files.pythonhosted.org/packages/.../chronotrace-VERSION-py3-none-any.whl"
```

Get that URL from `https://test.pypi.org/pypi/chronotrace/VERSION/json` (the `urls` array).

**Why not the obvious `--index-url https://test.pypi.org/simple/ --extra-index-url
https://pypi.org/simple/ chronotrace`?** Because `--index-url` makes TestPyPI the *primary*
index for **every** dependency, and TestPyPI is a public sandbox full of unrelated packages
squatting real names. Installing the `[ui]` extra that way fails with
`Failed to build 'fastapi'` — pip resolves TestPyPI's junk `fastapi` in preference to the real
one, and `--extra-index-url` does not save you, because it is a fallback rather than a
preference. The core install happens to work only because `zstandard` and `msgpack` have no
TestPyPI squatter.

Installing by URL sidesteps the whole problem: the artifact under test comes from TestPyPI, and
every dependency resolves from real PyPI, which is exactly the combination you want to verify.
None of this applies to the real PyPI release, where the plain `pip install chronotrace[ui]`
is what users run.

Then check the things a wheel can be silently wrong about:

```bash
/tmp/ct/bin/chronotrace --version          # reports [ui] extra: installed
/tmp/ct/bin/python -c "import chronotrace, pathlib; print((pathlib.Path(chronotrace.__file__).parent / '_ui' / 'index.html').is_file())"
```

That second line must print `True`. A wheel built from the sdist is API-only and will print
`False` — `chronotrace serve` would then start and serve no UI, which is the failure mode the
release job's packaging assertions exist to prevent.

---

## When something fails

### `guard` fails: tag does not match the version

Nothing was published. Delete the tag, fix, re-tag:

```bash
git tag -d v0.1.0 && git push --delete origin v0.1.0
```

### `guard` fails: version already on the index

The version is already published and indexes are immutable. There is no fix but a new version
number — bump, changelog, re-tag. **Do not** try to delete the release on PyPI: deleting a
version does not free it for re-upload, it burns it permanently.

### `build` fails

Nothing was published. Fix on `main`, delete and re-push the tag.

### `publish` fails

Read the error before doing anything:

- **OIDC / "not a trusted publisher"** — nothing was published. The publisher config does not
  match; fix it against the table above and re-run the job. This is the common one, and it is
  always a config mismatch, never a code problem.
- **"File already exists"** — the version is already on the index. See above; it cannot be
  overwritten.
- **Uploaded some files, then failed** — this is the partial state. PyPI accepts files
  individually, so you can end up with a wheel published and the sdist not. **The version is now
  burned.** Do not attempt to complete it by hand. Bump to the next patch version, changelog it
  honestly ("0.1.1 — 0.1.0 was incompletely published"), and release that. Yank the incomplete
  version on PyPI so nobody installs it: `pip` will then skip it unless pinned exactly.

### `image` fails (after PyPI succeeded)

**The release is valid.** The package is on PyPI and that is the thing users install; only the
demo image is missing. Do not re-tag. Fix, then push the image on its own:

```bash
docker build -t ghcr.io/dharmppp21/chronotrace:0.1.0 .
./tests/packaging/smoke_image.sh ghcr.io/dharmppp21/chronotrace:0.1.0
docker push ghcr.io/dharmppp21/chronotrace:0.1.0
```

Common causes: the Node stage failing (a frontend dependency broke), or the multi-stage
boundary being wrong so the final image is enormous.

### `github-release` fails (after PyPI succeeded)

Also not fatal. Create it by hand:

```bash
gh release create v0.1.0 --title "ChronoTrace v0.1.0" --notes-file <(...) dist/*
```

---

## Why it is built this way

- **The tag and the version are independent sources.** `_version.py` holds a literal instead of
  deriving itself from git, *specifically* so `guard` compares two things that can disagree.
  Derive one from the other and the assertion becomes a tautology that passes unconditionally
  while proving nothing.
- **Actions are pinned to commit SHAs, not tags.** A tag like `v4` is mutable: whoever controls
  the action can re-point it at new code, which then runs inside this pipeline with its
  permissions. A SHA cannot be re-pointed. Dependabot proposes the bumps so pinning does not
  quietly mean "never updated".
- **Permissions are per job, not per workflow.** `build` cannot publish. `publish` cannot write
  to the repository. `image` cannot do either.
- **An SBOM ships with every release** because ChronoTrace is imported into the user's process
  and reads their program's memory. Asking someone to install that carries a higher duty of care
  than most packages, and a machine-readable dependency inventory is part of paying it.
