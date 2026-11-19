#!/usr/bin/env bash
# freemilk-bridge installer — idempotent, zero dependencies.
#
# Renders the systemd unit for the CURRENT user and THIS directory,
# installs it, enables and (re)starts the service. Safe to re-run:
# it just re-renders and restarts. Without systemd/sudo it prints the
# no-systemd fallback (./run.sh) instead of failing.
#
#   bash install.sh
#
# Overrides: FREEMILK_BRIDGE_PORT (default 18611).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${FREEMILK_BRIDGE_PORT:-18611}"
PYTHON_BIN="$(command -v python3 || true)"

if [ -z "$PYTHON_BIN" ]; then
  echo "install: python3 not found in PATH — install Python 3.8+ first" >&2
  exit 1
fi

UNIT_SRC="$DIR/freemilk-bridge.service"
UNIT_TMP="$(mktemp)"
sed -e "s|__USER__|$(id -un)|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__DIR__|$DIR|g" \
    -e "s|__PYTHON__|$PYTHON_BIN|g" \
    -e "s|FREEMILK_BRIDGE_PORT=18611|FREEMILK_BRIDGE_PORT=$PORT|" \
    "$UNIT_SRC" > "$UNIT_TMP"

install_systemd() {
  sudo install -m 0644 "$UNIT_TMP" /etc/systemd/system/freemilk-bridge.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now freemilk-bridge
  sudo systemctl restart freemilk-bridge
}

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
   && sudo -n true 2>/dev/null; then
  install_systemd
  echo "install: freemilk-bridge service enabled + started (port $PORT)"
elif command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  echo "install: sudo needs a password here; re-run these commands:"
  echo "  sudo install -m 0644 '$UNIT_TMP' /etc/systemd/system/freemilk-bridge.service"
  echo "  sudo systemctl daemon-reload && sudo systemctl enable --now freemilk-bridge"
  echo "install: (unit rendered at $UNIT_TMP — not deleting it)"
  UNIT_KEEP=1
else
  echo "install: no systemd available — start the bridge with:"
  echo "  ./run.sh"
fi

for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "install: bridge healthy at http://127.0.0.1:$PORT/health"
    break
  fi
  sleep 0.5
done

echo "next: python3 login_fetch.py login   (or: login_fetch.py bulk-add <file>)"
echo "then: python3 login_fetch.py sync-9router"
[ "${UNIT_KEEP:-0}" = "1" ] || rm -f "$UNIT_TMP"
