#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
HERDR_BIN="${HERDR_BIN_PATH:-$(command -v herdr || true)}"

python3 "$ROOT/scripts/install_keybind.py" uninstall || true

if [[ -n "$HERDR_BIN" ]]; then
  "$HERDR_BIN" plugin action invoke codex.scheduler.stop-daemon >/dev/null 2>&1 || true
  sleep 0.2
  "$HERDR_BIN" plugin unlink codex.scheduler || true
else
  python3 "$ROOT/scripts/daemon.py" stop || true
fi

echo "Codex & Claude Scheduler unlinked. Job/history state is intentionally kept in Herdr's plugin state directory."
