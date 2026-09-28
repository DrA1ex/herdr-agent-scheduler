#!/bin/bash
set -euo pipefail

HERDR="${HERDR_BIN_PATH:-herdr}"
TARGET="${HERDR_PANE_ID:-${HERDR_ACTIVE_PANE_ID:-}}"

if [[ -z "$TARGET" && -n "${HERDR_PLUGIN_CONTEXT_JSON:-}" ]]; then
  TARGET="$(python3 - <<'PY'
import json, os
try:
    ctx = json.loads(os.environ.get('HERDR_PLUGIN_CONTEXT_JSON') or '{}')
except Exception:
    ctx = {}
print(ctx.get('focused_pane_id') or ctx.get('pane_id') or '')
PY
)"
fi

args=(plugin pane open --plugin codex.scheduler --entrypoint manager --placement popup --width '86%' --height '78%' --focus)
if [[ -n "$TARGET" ]]; then
  args+=(--env "CODEX_SCHED_TARGET_PANE=$TARGET")
fi
exec "$HERDR" "${args[@]}"
