#!/usr/bin/env bash
# Rebuilds the React dashboard and stages it where the gateway actually
# serves it from, then (unless --no-restart) restarts a locally running
# gateway so the new build takes effect.
#
# Why this needs to exist: the gateway serves the dashboard from the BUILT
# bundle at src/kiro_crew/static/dist, never from website/src directly (see
# _DIST_DIR in src/kiro_crew/dashboard/server.py). Editing website/src/*.tsx
# does nothing to a running gateway until that bundle is rebuilt and staged -
# install.sh does this once at install time, but a local dev loop that only
# edits source and restarts the gateway will keep serving a stale build and
# silently drop every frontend change (a whole session's worth, once).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_SRC="$ROOT/website/dist"
DIST_DST="$ROOT/src/kiro_crew/static/dist"
PORT="${KIROCREW_PORT:-5476}"
RESTART=1

for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=0 ;;
    *)
      echo "usage: $(basename "$0") [--no-restart]" >&2
      exit 2
      ;;
  esac
done

echo "Building frontend (website/)…"
(
  cd "$ROOT/website"
  if [ -f package-lock.json ]; then
    npm ci --no-audit --no-fund --loglevel=error
  else
    npm install --no-audit --no-fund --loglevel=error
  fi
  NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=8192}" npm run build
)

[ -f "$DIST_SRC/index.html" ] || { echo "build produced no $DIST_SRC/index.html - aborting stage" >&2; exit 1; }

echo "Staging $DIST_SRC -> $DIST_DST"
rm -rf "$DIST_DST"
mkdir -p "$(dirname "$DIST_DST")"
cp -R "$DIST_SRC" "$DIST_DST"
echo "staged: $(ls "$DIST_DST" | wc -l | tr -d ' ') entries"

if [ "$RESTART" != "1" ]; then
  echo "Skipping gateway restart (--no-restart). Restart it yourself to pick up the new build."
  exit 0
fi

echo "Restarting gateway on port $PORT…"
pkill -f "kirocrew gateway" 2>/dev/null || true
sleep 1
rm -f "$HOME/.kiro/crew/gateway.lock" 2>/dev/null || true

(
  cd "$ROOT"
  # shellcheck disable=SC1091
  source .venv/bin/activate
  nohup kirocrew gateway --port "$PORT" >/tmp/kirocrew-gateway.log 2>&1 &
  disown
)

for _ in $(seq 1 15); do
  if curl -sf "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
    echo "gateway is up on port $PORT, serving the fresh build."
    exit 0
  fi
  sleep 1
done

echo "gateway did not come up within 15s - check /tmp/kirocrew-gateway.log" >&2
exit 1
