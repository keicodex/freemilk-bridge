#!/usr/bin/env bash
# freemilk-bridge without systemd: run the bridge in the foreground.
# Zero dependencies (Python 3 standard library only).
#
#   ./run.sh
#
# Overrides: FREEMILK_BRIDGE_HOST / FREEMILK_BRIDGE_PORT /
# FREEMILK_BRIDGE_CREDENTIALS / BRIDGE_API_KEY.
set -euo pipefail
cd "$(dirname "$0")"
export FREEMILK_BRIDGE_HOST="${FREEMILK_BRIDGE_HOST:-127.0.0.1}"
export FREEMILK_BRIDGE_PORT="${FREEMILK_BRIDGE_PORT:-18611}"
export FREEMILK_BRIDGE_CREDENTIALS="${FREEMILK_BRIDGE_CREDENTIALS:-$PWD/credentials.json}"
exec python3 bridge.py
