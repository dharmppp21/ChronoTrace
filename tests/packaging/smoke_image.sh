#!/usr/bin/env bash
# Smoke-test the demo image. Asserts the promises the README makes about it, so that
# "docker run ... and you are scrubbing a real bug" is a checked claim rather than a hope.
#
#   ./tests/packaging/smoke_image.sh chronotrace-demo
#
# Every check below corresponds to a way the image could ship broken while still BUILDING
# fine -- which is exactly the class of bug a build alone cannot catch.
set -euo pipefail

IMAGE="${1:?usage: smoke_image.sh IMAGE}"
NAME="chronotrace-smoke-$$"
PORT=8000

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. Non-root. A container serving HTTP to a browser has no reason to run as root, and
#    `USER chrono` is the kind of line that silently stops working after a stage is reordered.
uid="$(docker run --rm "$IMAGE" id -u)"
[ "$uid" != "0" ] || fail "image runs as root (uid 0); expected the unprivileged chrono user"
echo "ok: runs as non-root (uid $uid)"

# 2. No Node in the final image. This is the multi-stage boundary holding. If it leaks, the
#    image balloons and ships a JS toolchain to people who only wanted to look at a recording.
if docker run --rm "$IMAGE" sh -c 'command -v node' >/dev/null 2>&1; then
  fail "node leaked into the runtime image -- the multi-stage boundary is wrong"
fi
echo "ok: no Node in the runtime image"

# 3. The demo recording is baked in. If this is missing the container starts and serves an
#    empty directory, which looks like a working demo right up until the reader clicks.
docker run --rm "$IMAGE" test -f /demo/demo.chrono || fail "/demo/demo.chrono is missing"
echo "ok: demo recording is baked in"

# 4. It actually serves. Start it the way a user would, then poll -- uvicorn needs a moment,
#    and a fixed sleep is either flaky or slow.
docker run -d --name "$NAME" -p "$PORT:8000" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:$PORT/api/sessions" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[ "${ready:-0}" = "1" ] || { docker logs "$NAME" >&2; fail "server did not answer on :$PORT in 30s"; }
echo "ok: API answers on :$PORT"

# 5. The recording is actually LISTED. Serving a directory the app cannot read would pass
#    check 4 and still be a dead demo -- this is the check that the reader's first click works.
curl -fsS "http://localhost:$PORT/api/sessions" | grep -q demo \
  || fail "the demo recording is not listed by /api/sessions"
echo "ok: demo session is listed"

# 6. The bundled UI is served at /. Without this the reader gets JSON, not a scrubber.
curl -fsS "http://localhost:$PORT/" | grep -qi '<div id="root"\|<!doctype html' \
  || fail "the UI is not served at / -- the wheel may be API-only (no _ui/ bundled)"
echo "ok: UI is served at /"

echo "demo image OK: non-root, no Node, recording baked in, UI served."
