#!/usr/bin/env bash
# Phase 2 end-to-end demo: three-task fan-out (backend API -> frontend + QA in parallel).
#
# Prerequisites (all must be satisfied before running):
#   make up           -- Postgres + Redis running
#   make migrate      -- schema applied
#   uvicorn orchestrator.orchestrator.api:app --port 8080  (separate terminal)
#   uvicorn gateway.gateway.app:app --port 8081            (separate terminal)
#   ANTHROPIC_API_KEY set in environment or .env
#
# Usage:
#   uv run bash scripts/demo_v2.sh
#   OR
#   make demo-v2

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$ROOT/sandbox/sample-project"
ORCH_URL="${ORCHESTRATOR_URL:-http://localhost:8080}"
GW_URL="${GATEWAY_URL:-http://localhost:8081}"
STORE_DIR="${RUN_STORE_DIR:-/tmp/orchestra/runs}"
POLL_TIMEOUT=300  # seconds to wait for agents to complete

sep() { echo ""; echo "--- $* ---"; }

# ── 0. Preflight ─────────────────────────────────────────────────────────────

sep "Preflight checks"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set." >&2
    exit 1
fi

if ! curl -sf "$ORCH_URL/healthz" > /dev/null 2>&1; then
    echo "ERROR: Orchestrator not running at $ORCH_URL" >&2
    echo "  Start with: uv run uvicorn orchestrator.orchestrator.api:app --port 8080" >&2
    exit 1
fi
echo "Orchestrator: OK"

if ! curl -sf "$GW_URL/healthz" > /dev/null 2>&1; then
    echo "ERROR: Gateway not running at $GW_URL" >&2
    echo "  Start with: uv run uvicorn gateway.gateway.app:app --port 8081" >&2
    exit 1
fi
echo "Gateway: OK"

# ── 1. Sample project git setup ───────────────────────────────────────────────

sep "Sample project git setup"

cd "$REPO"
if [ ! -d ".git" ]; then
    git init -b main
    git config user.email "demo@orchestra"
    git config user.name "Orchestra Demo"
    git add .
    git commit -m "chore: initial sample project"
    echo "Initialised git repo."
else
    git checkout main 2>/dev/null || true
    echo "Git repo already initialised."
fi
cd "$ROOT"

# ── 2. Create the three tasks ─────────────────────────────────────────────────

sep "Creating tasks"

# Task 1: backend writes the API spec
BE_OUT=$(uv run orchctl create-task \
    "Write API specification for /items endpoint" \
    --owner "backend-agent" \
    --accept "api_spec.md exists and contains endpoint definition" \
    --output "api_spec.md" \
    --risk-tier 1)
echo "$BE_OUT"
BE_ID=$(echo "$BE_OUT" | grep "^Created" | awk '{print $2}' | tr -d ':')
echo "Backend task: $BE_ID"

# Task 2: frontend reads the spec, produces a consumption layer
FE_OUT=$(uv run orchctl create-task \
    "Implement frontend index page consuming /items" \
    --owner "frontend-agent" \
    --depends-on "$BE_ID" \
    --input "api_spec.md" \
    --output "frontend/index.md" \
    --risk-tier 1)
echo "$FE_OUT"
FE_ID=$(echo "$FE_OUT" | grep "^Created" | awk '{print $2}' | tr -d ':')
echo "Frontend task: $FE_ID"

# Task 3: QA reads the spec, files a report
QA_OUT=$(uv run orchctl create-task \
    "QA review of /items API spec" \
    --owner "qa-agent" \
    --depends-on "$BE_ID" \
    --input "api_spec.md" \
    --output "qa_report.md" \
    --risk-tier 1)
echo "$QA_OUT"
QA_ID=$(echo "$QA_OUT" | grep "^Created" | awk '{print $2}' | tr -d ':')
echo "QA task: $QA_ID"

# ── 3. Approve and run the backend task ───────────────────────────────────────

sep "Approving backend task ($BE_ID)"
uv run orchctl approve "$BE_ID"

sep "Starting backend run ($BE_ID)"
RUN_OUT=$(uv run orchctl run-task "$BE_ID" --repo "$REPO" --agent-id "backend-agent")
echo "$RUN_OUT"
RUN_ID=$(echo  "$RUN_OUT" | grep "run_id"  | awk '{print $2}')
CONTEXT=$(echo "$RUN_OUT" | grep "context" | awk '{print $2}')

sep "Running backend agent"
uv run python -m agents.backend.main \
    --context "$CONTEXT" \
    --run-id  "$RUN_ID" \
    --repo    "$REPO"

# ── 4. Validate backend task ──────────────────────────────────────────────────

sep "Validating backend task ($BE_ID)"
uv run orchctl validate "$BE_ID" --repo "$REPO"

# ── 5. Dispatcher auto-assigns FE and QA ─────────────────────────────────────
#
# The dispatcher reacts to TASK_COMPLETED / TASK_VALIDATED events and transitions
# both FE and QA to 'assigned'. Run the dispatcher briefly if it is not already
# running, or rely on it running in the background.

sep "Waiting for dispatcher to fan out ($FE_ID and $QA_ID -> assigned)"
ELAPSED=0
while [ $ELAPSED -lt $POLL_TIMEOUT ]; do
    FE_STATUS=$(curl -sf "$ORCH_URL/tasks/$FE_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    QA_STATUS=$(curl -sf "$ORCH_URL/tasks/$QA_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    echo "  $FE_ID=$FE_STATUS  $QA_ID=$QA_STATUS  (${ELAPSED}s elapsed)"
    if [ "$FE_STATUS" != "created" ] && [ "$QA_STATUS" != "created" ]; then
        echo "Both tasks unblocked by dispatcher."
        break
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

if [ "$FE_STATUS" = "created" ] || [ "$QA_STATUS" = "created" ]; then
    echo "ERROR: Dispatcher did not fan out within ${POLL_TIMEOUT}s." >&2
    echo "  Make sure the dispatcher is running: make dispatcher" >&2
    exit 1
fi

# ── 6. Run frontend and QA agents (if not already launched by dispatcher) ─────

# If the dispatcher is running it will have launched the agents automatically.
# If running the demo manually without the dispatcher, run them here.

if [ "${RUN_AGENTS_MANUALLY:-0}" = "1" ]; then
    sep "Running frontend agent manually"
    FE_RUN_OUT=$(uv run orchctl run-task "$FE_ID" --repo "$REPO" --agent-id "frontend-agent")
    echo "$FE_RUN_OUT"
    FE_RUN_ID=$(echo "$FE_RUN_OUT" | grep "run_id"  | awk '{print $2}')
    FE_CTX=$(echo    "$FE_RUN_OUT" | grep "context" | awk '{print $2}')
    uv run python -m agents.frontend.main \
        --context "$FE_CTX" --run-id "$FE_RUN_ID" --repo "$REPO" &

    sep "Running QA agent manually"
    QA_RUN_OUT=$(uv run orchctl run-task "$QA_ID" --repo "$REPO" --agent-id "qa-agent")
    echo "$QA_RUN_OUT"
    QA_RUN_ID=$(echo "$QA_RUN_OUT" | grep "run_id"  | awk '{print $2}')
    QA_CTX=$(echo    "$QA_RUN_OUT" | grep "context" | awk '{print $2}')
    uv run python -m agents.qa.main \
        --context "$QA_CTX" --run-id "$QA_RUN_ID" --repo "$REPO" &

    wait
fi

# ── 7. Poll for both agents to complete ───────────────────────────────────────

sep "Waiting for frontend and QA agents to complete"
ELAPSED=0
while [ $ELAPSED -lt $POLL_TIMEOUT ]; do
    FE_STATUS=$(curl -sf "$ORCH_URL/tasks/$FE_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    QA_STATUS=$(curl -sf "$ORCH_URL/tasks/$QA_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    echo "  $FE_ID=$FE_STATUS  $QA_ID=$QA_STATUS  (${ELAPSED}s elapsed)"
    DONE=1
    for s in "$FE_STATUS" "$QA_STATUS"; do
        case "$s" in
            completed|validated|merged|closed) ;;
            failed|escalated) echo "ERROR: task entered $s"; exit 1 ;;
            *) DONE=0 ;;
        esac
    done
    [ $DONE -eq 1 ] && break
    sleep 10
    ELAPSED=$((ELAPSED + 10))
done

if [ $DONE -ne 1 ]; then
    echo "ERROR: Agents did not complete within ${POLL_TIMEOUT}s." >&2
    exit 1
fi

# ── 8. Validate and merge frontend + QA tasks ─────────────────────────────────

sep "Validating frontend task ($FE_ID)"
uv run orchctl validate "$FE_ID" --repo "$REPO" || true

sep "Merging frontend task ($FE_ID)"
uv run orchctl merge "$FE_ID" --repo "$REPO" || true

sep "Validating QA task ($QA_ID)"
uv run orchctl validate "$QA_ID" --repo "$REPO" || true

sep "Merging QA task ($QA_ID)"
uv run orchctl merge "$QA_ID" --repo "$REPO" || true

# ── 9. Summary ────────────────────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════"
echo " Phase 2 fan-out demo complete"
echo "════════════════════════════════════════"
echo ""
echo "Task status:"
uv run orchctl list
echo ""
echo "Sample project git log:"
git -C "$REPO" log --oneline
