#!/usr/bin/env bash
# Phase 1 end-to-end demo: add GET /health to the sample FastAPI project.
#
# Prerequisites (all must be satisfied before running):
#   make up           — Postgres running on port 5433
#   make migrate      — schema applied
#   uvicorn orchestrator.orchestrator.api:app --port 8080  (in a separate terminal)
#   uvicorn gateway.gateway.app:app --port 8081            (in a separate terminal)
#   ANTHROPIC_API_KEY set in environment or .env
#
# Usage:
#   uv run bash scripts/demo.sh
#   OR
#   make demo

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$ROOT/sandbox/sample-project"
ORCH_URL="${ORCHESTRATOR_URL:-http://localhost:8080}"
GW_URL="${GATEWAY_URL:-http://localhost:8081}"

sep() { echo ""; echo "--- $* ---"; }

# ── 0. Preflight ────────────────────────────────────────────────────────────

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

# ── 1. Initialise sample project as a git repo ──────────────────────────────

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
    # Ensure we are on main and the tree is clean.
    git checkout main 2>/dev/null || true
    echo "Git repo already initialised."
fi
cd "$ROOT"

# ── 2. Create task ───────────────────────────────────────────────────────────

sep "Creating task"

CREATE_OUT=$(uv run orchctl create-task \
    "Add GET /health endpoint" \
    --owner "backend-agent" \
    --accept "GET /health returns 200 with JSON body containing status key" \
    --accept "pytest passes" \
    --input "app/main.py" \
    --output "app/main.py" \
    --output "tests/test_health.py")
echo "$CREATE_OUT"

TASK_ID=$(echo "$CREATE_OUT" | grep "^Created" | awk '{print $2}' | tr -d ':')
echo "Task ID: $TASK_ID"

# ── 3. Approve: created → assigned ──────────────────────────────────────────

sep "Approving task (created → assigned)"
uv run orchctl approve "$TASK_ID"

# ── 4. Start run: assigned → running ────────────────────────────────────────

sep "Starting run (assigned → running)"
RUN_OUT=$(uv run orchctl run-task "$TASK_ID" --repo "$REPO")
echo "$RUN_OUT"

RUN_ID=$(echo   "$RUN_OUT" | grep "run_id"  | awk '{print $2}')
CONTEXT=$(echo  "$RUN_OUT" | grep "context" | awk '{print $2}')

# ── 5. Run backend agent: running → completed ────────────────────────────────

sep "Running backend agent (running → completed)"
uv run python -m agents.backend.main \
    --context "$CONTEXT" \
    --run-id  "$RUN_ID" \
    --repo    "$REPO"

# ── 6. Validate: completed → validated ──────────────────────────────────────

sep "Validating agent branch (completed → validated)"
uv run orchctl validate "$TASK_ID" --repo "$REPO"

# ── 7. Merge: validated → merged → closed ───────────────────────────────────

sep "Merging branch into main (validated → merged → closed)"
uv run orchctl merge "$TASK_ID" --repo "$REPO"

# ── 8. Summary ───────────────────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════"
echo " Phase 1 demo complete"
echo "════════════════════════════════════════"
echo ""
echo "Task status:"
uv run orchctl list
echo ""
echo "Sample project git log:"
git -C "$REPO" log --oneline
