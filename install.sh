#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
HERDR_BIN="${HERDR_BIN_PATH:-$(command -v herdr || true)}"

if [[ -z "$HERDR_BIN" ]]; then
  echo "codex-scheduler: herdr not found in PATH" >&2
  exit 1
fi

version="$($HERDR_BIN --version 2>/dev/null || true)"
echo "Using: ${version:-$HERDR_BIN}"

for agent in codex claude; do
  echo "Ensuring the official Herdr $agent integration is installed..."
  if ! "$HERDR_BIN" integration install "$agent"; then
    echo "agent-scheduler: warning: could not install/update the Herdr $agent integration." >&2
    echo "Session IDs may be unavailable until 'herdr integration install $agent' succeeds." >&2
  fi
done

# 0.1.x may have a detached daemon running from the old checkout. Stop it before
# relinking; state itself is per plugin id and is intentionally preserved.
"$HERDR_BIN" plugin action invoke codex.scheduler.stop-daemon >/dev/null 2>&1 || true
sleep 0.1
"$HERDR_BIN" plugin unlink codex.scheduler >/dev/null 2>&1 || true
"$HERDR_BIN" plugin link "$ROOT"
python3 "$ROOT/scripts/install_keybind.py" install --root "$ROOT"
"$HERDR_BIN" plugin action invoke codex.scheduler.start-daemon >/dev/null 2>&1 || true

echo
echo "Installed Codex & Claude Scheduler 0.4.0."
echo "Reload Herdr config (prefix+Shift+R) or detach/reattach the client, then use prefix+s."
echo "Focus a Codex or Claude Code pane to create, edit, cancel, delete, and inspect scheduled prompts."
echo "If prefix+s was already occupied, invoke:"
echo "  herdr plugin action invoke codex.scheduler.manage"
