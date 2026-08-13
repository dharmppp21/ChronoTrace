# The "try it" image. Not a deployment target -- nobody deploys a debugger.
#
#   docker run --rm -p 8000:8000 ghcr.io/dharmppp21/chronotrace
#
# It exists so a curious reader gets from "interesting README" to "I am scrubbing backward
# through a real bug" in one command, with no Python, no Node, and no install. That framing
# drives every choice below: the demo recording is baked in, the UI extra is installed by
# default (the opposite of the wheel's dependency-light default), and the container starts
# serving immediately instead of waiting to be told what to do.
#
# Three stages, because the final image must contain no build tooling:
#   ui      -- Node builds the SPA. Node exists ONLY here.
#   builder -- Python builds the wheel, reusing the assets from `ui`.
#   runtime -- installs that wheel and nothing else. No Node, no compiler, no npm cache.
#
# Verify the boundary held:  docker run --rm --entrypoint sh IMAGE -c 'command -v node || echo no-node'

# ---------------------------------------------------------------------------
# Stage 1: build the browser UI. Node lives and dies in this stage.
# ---------------------------------------------------------------------------
FROM node:26-slim AS ui
WORKDIR /build

# Lockfile first, in its own layer: dependencies change far less often than source, so an edit
# to a .ts file reuses this layer instead of reinstalling node_modules. `npm ci` (not install)
# installs the locked tree exactly -- the same reproducibility rule hatch_build.py follows.
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci

COPY frontend/ ./frontend/
# vite.config.ts sets outDir '../src/chronotrace/_ui', so this writes to /build/src/chronotrace/_ui.
RUN cd frontend && npm run build

# ---------------------------------------------------------------------------
# Stage 2: build the wheel, reusing the UI that stage 1 already built.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder
WORKDIR /build

COPY . .
COPY --from=ui /build/src/chronotrace/_ui ./src/chronotrace/_ui

# The escape hatch hatch_build.py already documents: assets are present, so do not invoke npm.
# Without this the hook would look for npm, not find it in a python image, and silently fall
# back to "prebuilt" anyway -- setting it makes the intent explicit rather than incidental.
ENV CHRONOTRACE_SKIP_UI_BUILD=1
# Clear dist/ first. The build context legitimately carries the maintainer's dist/ (the
# clean-install test in tests/packaging/ installs the wheel from there, so .dockerignore cannot
# exclude it), and a stale wheel left beside the fresh one would make the glob in the runtime
# stage ambiguous -- silently installing a months-old build. Removing it here means the wheel
# that ships is provably the one this stage just produced.
RUN rm -rf dist && pip install --no-cache-dir build && python -m build --wheel

# ---------------------------------------------------------------------------
# Stage 3: the image people actually run.
# ---------------------------------------------------------------------------
# Pinned by digest, not by tag. `python:3.12-slim` is a moving target -- the same Dockerfile
# would produce different images on different days, which defeats the point of publishing one.
# The digest makes the base immutable, and Dependabot (.github/dependabot.yml) proposes the bump
# so pinning does not quietly mean "never updated".
#
# Note the deliberate asymmetry with tests/packaging/Dockerfile.clean, which stays UNpinned:
# that file is a canary and *should* track upstream so an upstream break surfaces as a red
# nightly. This file is a published artifact and should be reproducible. Different jobs.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS runtime

# A fixed high UID, created before anything is installed. Running as root in a container that
# serves HTTP to a browser is an unnecessary risk, and `--user` at run time is something nobody
# remembers to pass. The UID is explicit so a bind-mounted volume has predictable ownership.
RUN useradd --create-home --uid 10001 chrono

WORKDIR /demo

# [ui] here on purpose. The wheel's default is dependency-light because the recorder is imported
# into the user's process; this image has the opposite job -- it is a browser demo, so the server
# extra is the point, not a cost.
COPY --from=builder /build/dist/*.whl /tmp/
# The wheel path is resolved into a variable before the extra is appended. Writing
# `pip install /tmp/*.whl[ui]` looks right and is not: to the shell, `[ui]` is a glob character
# class matching a single `u` or `i`, so the pattern silently stops matching the wheel at all.
RUN WHEEL="$(ls /tmp/*.whl)" && pip install --no-cache-dir "${WHEEL}[ui]" && rm -f /tmp/*.whl

# Bake the demo recording at BUILD time, not at start time. Recording takes a few seconds and
# would otherwise be paid on every `docker run`, in the exact moment a first-time reader is
# deciding whether this tool is worth their attention. The example is the repo's own buggy
# pipeline, so the demo shows a real bug rather than a toy loop -- and it is generated here
# rather than committed, so a binary .chrono blob never enters git.
COPY examples/buggy_pipeline.py /demo/buggy_pipeline.py
RUN chronotrace record --out /demo/demo.chrono /demo/buggy_pipeline.py \
    && chown -R chrono:chrono /demo

USER chrono
EXPOSE 8000

# --host 0.0.0.0 is required in a container: binding 127.0.0.1 would only be reachable from
# inside it. This trips the CLI's "you are exposing recordings" warning, which is correct and
# intentional here -- the container's whole filesystem is a throwaway demo, and the operator
# chose to publish the port. Host-header validation still applies (TrustedHostMiddleware accepts
# localhost/127.0.0.1, which is what the browser sends), so this is a bind change, not an
# authorisation change.
CMD ["chronotrace", "serve", "--dir", "/demo", "--host", "0.0.0.0", "--port", "8000"]
