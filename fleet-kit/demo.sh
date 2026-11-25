#!/usr/bin/env bash
# Fleet Kit 1.0 green path — no model API keys required.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PATH="${HOME}/.local/bin:${PATH}"
# Fresh random demo keys on every run: no guessable literals (B2). Exports the
# client keys FLEET_API_KEY, FLEET_OTHER_TENANT_KEY, FLEET_AGENT_A_KEY,
# FLEET_AGENT_B_KEY (a preset value of >= 24 chars is kept) and the Hub side
# AI_FLEET_API_KEY / AI_FLEET_TENANT_API_KEYS / AI_FLEET_AGENT_API_KEYS, which
# hold hashes only. Stdlib only, so it runs before pip install.
DEMO_KEYS="$(python3 scripts/gen_keys.py demo --shell)"
eval "$DEMO_KEYS"
unset DEMO_KEYS
PORT="${FLEET_PORT:-8765}"
# Keep URL in sync with the listening port unless the caller set both.
if [[ -z "${FLEET_HUB_URL:-}" ]]; then
  export FLEET_HUB_URL="http://127.0.0.1:${PORT}"
fi
DB_PATH="${ROOT}/.demo-fleet.db"
rm -f "$DB_PATH"

export AI_FLEET_DB="sqlite:///${DB_PATH}"

# CI installs dependencies itself and sets FLEET_SKIP_PIP=1.
if [[ "${FLEET_SKIP_PIP:-0}" != "1" ]]; then
  python3 -m pip install --user -q -r requirements.txt
fi

HUB_LOG="${ROOT}/.demo-hub.log"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$HUB_LOG" 2>&1 &
HUB_PID=$!
cleanup() { kill "$HUB_PID" 2>/dev/null || true; }
trap cleanup EXIT

for i in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null; then
    break
  fi
  sleep 0.1
done
curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null

python3 scripts/demo_two_agents.py | tee .demo-out.txt
TASK_ID=$(python3 -c "import re,sys; t=open('.demo-out.txt').read(); m=re.search(r'task_id=(\\S+)', t); print(m.group(1) if m else '')")
export FLEET_DEMO_TASK_ID="$TASK_ID"
export FLEET_API_KEY FLEET_AGENT_A_KEY FLEET_AGENT_B_KEY
export FLEET_AGENT_API_KEY="$FLEET_AGENT_A_KEY"
# MCP path runs without the tenant admin key: hosts carry agent keys only.
env -u FLEET_API_KEY python3 scripts/mcp_http_parity.py

echo "demo.sh PASS"
