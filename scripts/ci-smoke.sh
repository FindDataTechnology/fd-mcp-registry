#!/usr/bin/env bash
# Repo-owned smoke gate for the generic image pool (fd-build-snapshot).
#
# The pool's default "boot" mode runs the image bare. That is wrong for this
# image: registry-entrypoint.sh fails fast without SECRET_KEY ("ERROR: SECRET_KEY
# environment variable is required but not set") — a deliberate guard, not a
# defect. The pool cannot inject env (it does not know any image's contract),
# so the repo carries its own gate.
#
# Contract: "$1" = fully-qualified image ref (hk registry + tag).
# Exit 0 = booted and stayed up; anything else = the gate failed.
set -euo pipefail

IMG="${1:?usage: ci-smoke.sh <image-ref>}"
NAME="pool-smoke-registry"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# A throwaway key: this container never serves traffic and never sees real
# session cookies. It only satisfies the entrypoint's fail-fast guard.
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

cleanup
docker run -d --name "$NAME" \
  -e "SECRET_KEY=${SECRET_KEY}" \
  -e "HOST=0.0.0.0" \
  -e "PORT=7860" \
  -e "APP_LOG_MONGODB_ENABLED=false" \
  "$IMG" >/dev/null

# Boot gate: the process must survive its startup sequence. Mongo is absent in
# the runner; the registry tolerates that at boot (it retries), which is the
# same shape the cluster sees during a cold start.
for i in $(seq 1 9); do
  sleep 5
  if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "SMOKE FAILED - container exited within ${i}x5s; logs follow" >&2
    docker logs "$NAME" 2>&1 | tail -40 >&2 || true
    exit 1
  fi
done

echo "smoke: registry booted and stayed up for 45s (SECRET_KEY supplied by repo gate)"
