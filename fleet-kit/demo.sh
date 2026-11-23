#!/usr/bin/env bash
# Fleet Kit 1.0 green path — no model API keys required.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PATH="${HOME}/.local/bin:${PATH}"
export FLEET_API_KEY="${FLEET_API_KEY:-fleet-demo-tenant}"
export FLEET_OTHER_TENANT_KEY="${FLEET_OTHER_TENANT_KEY:-fleet-demo-other}"
export FLEET_AGENT_A_KEY="${FLEET_AGENT_A_KEY:-fleet-demo-orch}"
export FLEET_AGENT_B_KEY="${FLEET_AGENT_B_KEY:-fleet-demo-worker}"
export FLEET_HUB_URL="${FLEET_HUB_URL:-http://127.0.0.1:8765}"
PORT="${FLEET_PORT:-8765}"
DB_PATH="${ROOT}/.demo-fleet.db"
rm -f "$DB_PATH"

export AI_FLEET_DB="sqlite:///${DB_PATH}"
export AI_FLEET_API_KEY="$FLEET_API_KEY"
export AI_FLEET_TENANT_API_KEYS=$(python3 - <<PY
import json, os
print(json.dumps({
  os.environ["FLEET_API_KEY"]: "demo-tenant",
  os.environ["FLEET_OTHER_TENANT_KEY"]: "other-tenant",
}))
PY
)
export AI_FLEET_AGENT_API_KEYS=$(python3 - <<PY
import json, os
print(json.dumps({
  os.environ["FLEET_AGENT_A_KEY"]: {"agent_id": "orchestrator", "tenant_id": "demo-tenant"},
  os.environ["FLEET_AGENT_B_KEY"]: {"agent_id": "worker", "tenant_id": "demo-tenant"},
}))
PY
)

python3 -m pip install --user -q -r requirements.txt

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
python3 scripts/mcp_http_parity.py

echo "demo.sh PASS"
