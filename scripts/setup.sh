#!/usr/bin/env bash
# Orchestra setup script — takes a fresh clone to a fully running platform.
#
# Usage:
#   bash scripts/setup.sh                        # interactive menu
#   bash scripts/setup.sh --spec diary_spec.md   # auto: generate tasks from spec via LLM
#   bash scripts/setup.sh --plan tasks.json      # auto: submit a pre-built task JSON
#
# The script is idempotent: re-running skips steps already done.

set -euo pipefail

# ── Parse args ────────────────────────────────────────────────────────────────
SPEC=""
PLAN=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --spec) SPEC="$2"; shift 2 ;;
    --plan) PLAN="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${SANDBOX_REPO_PATH:-$ROOT/sandbox/sample-project}"
LOG_DIR="/tmp/orchestra/logs"
PID_DIR="/tmp/orchestra/pids"
RUN_STORE_DIR="${RUN_STORE_DIR:-/tmp/orchestra/runs}"
ORCH_URL="${ORCHESTRATOR_URL:-http://localhost:8080}"
GW_URL="${GATEWAY_URL:-http://localhost:8081}"

mkdir -p "$LOG_DIR" "$PID_DIR" "$RUN_STORE_DIR"

# ── Terminal helpers ──────────────────────────────────────────────────────────
_bold()  { printf '\033[1m%s\033[0m' "$*"; }
_dim()   { printf '\033[2m%s\033[0m' "$*"; }
_green() { printf '\033[32m%s\033[0m' "$*"; }
_cyan()  { printf '\033[36m%s\033[0m' "$*"; }
sep()    { echo ""; echo "  $(_dim "────────────────────────────────────────")"; echo "  $*"; }

# ── Welcome screen ────────────────────────────────────────────────────────────
clear
cat << 'BANNER'

   ___  ____   ____ _   _ _____ ____ _____ ____      _
  / _ \|  _ \ / ___| | | | ____/ ___|_   _|  _ \    / \
 | | | | |_) | |   | |_| |  _| \___ \ | | | |_) |  / _ \
 | |_| |  _ <| |___| |_| | |___ ___) || | |  _ <  / ___ \
  \___/|_| \_\\____|\___/|_____|____/ |_| |_| \_\/_/   \_\

  Human-Centric Multi-Agent Orchestration Platform

BANNER

echo "  $(_bold 'How it works')"
echo ""
echo "  $(_cyan 'You')           own intent — you define the goal, approve work, merge results"
echo "  $(_cyan 'Agents')        own execution — specialist LLM workers (backend / frontend / QA)"
echo "  $(_cyan 'Orchestrator')  owns governance — task DAG, event bus, audit log"
echo ""
echo "  $(_dim '┌─────────────────────────────────────────────────────────────────┐')"
echo "  $(_dim '│')  You ──► Planner ──► Orchestrator (8080) ──► Dispatcher        $(_dim '│')"
echo "  $(_dim '│')                              │                    │              $(_dim '│')"
echo "  $(_dim '│')                         Event bus (Redis)    Gateway (8081)     $(_dim '│')"
echo "  $(_dim '│')                              │                    │              $(_dim '│')"
echo "  $(_dim '│')                         Agents ◄────────────────►Sandbox repo   $(_dim '│')"
echo "  $(_dim '└─────────────────────────────────────────────────────────────────┘')"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
sep "Checking prerequisites"

for cmd in python3 uv docker git curl; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "  ERROR: '$cmd' not found." >&2; exit 1
  fi
done

if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"; then
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  echo "  Python $PY_VER: $(_green 'OK')"
else
  echo "  ERROR: Python 3.12+ required." >&2; exit 1
fi

# ── 2. Python packages ────────────────────────────────────────────────────────
sep "Installing Python packages"
cd "$ROOT"
uv sync --quiet
echo "  uv sync: $(_green 'OK')"

# ── 3. .env bootstrap ─────────────────────────────────────────────────────────
sep "Configuring .env"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "  Created .env from .env.example"
fi

if ! grep -qE "^ANTHROPIC_API_KEY=.+" "$ROOT/.env"; then
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}|" "$ROOT/.env"
    echo "  ANTHROPIC_API_KEY: written from environment"
  else
    echo ""
    printf "  Enter your Anthropic API key (sk-ant-...): "
    read -r _key
    sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${_key}|" "$ROOT/.env"
    echo "  ANTHROPIC_API_KEY: saved"
  fi
fi

set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
echo "  .env loaded"

# ── 4. Docker infrastructure ───────────────────────────────────────────────────
sep "Starting Docker services  $(_dim '(Postgres 5433 · Redis 6380)')"
make -C "$ROOT" up

echo -n "  Waiting for Postgres..."
for i in $(seq 30); do
  if docker exec orchestra-postgres-1 pg_isready -U orchestra -q 2>/dev/null; then
    echo " $(_green 'ready')"; break
  fi
  sleep 1
  [ "$i" -eq 30 ] && { echo ""; echo "  ERROR: Postgres timeout." >&2; exit 1; }
done

echo -n "  Waiting for Redis..."
for i in $(seq 30); do
  if docker exec orchestra-redis-1 redis-cli ping 2>/dev/null | grep -q "PONG"; then
    echo " $(_green 'ready')"; break
  fi
  sleep 1
  [ "$i" -eq 30 ] && { echo ""; echo "  ERROR: Redis timeout." >&2; exit 1; }
done

# ── 5. Migrations ─────────────────────────────────────────────────────────────
sep "Applying database migrations"
make -C "$ROOT" migrate > /dev/null
echo "  Migrations: $(_green 'OK')"

# ── 6. Sandbox git repo ───────────────────────────────────────────────────────
sep "Initialising sandbox project repo"
if [ ! -d "$REPO/.git" ]; then
  cd "$REPO"
  git init -b main
  git config user.email "demo@orchestra"
  git config user.name "Orchestra Demo"
  git add .
  git commit -m "chore: initial sample project" -q
  cd "$ROOT"
  echo "  Initialised at $REPO"
else
  echo "  Already initialised"
fi

# ── 7. Background services ────────────────────────────────────────────────────
sep "Starting platform services"

_start_service() {
  local name="$1"; shift
  local pid_file="$PID_DIR/$name.pid"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "  $name: already running (pid $(cat "$pid_file"))"
    return
  fi
  rm -f "$pid_file"
  "$@" >> "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$pid_file"
  echo "  $name: started $(_dim "(pid $!, log: $LOG_DIR/$name.log)")"
}

cd "$ROOT"
_start_service orchestrator uv run uvicorn orchestrator.orchestrator.api:app --port 8080
_start_service gateway      uv run uvicorn gateway.gateway.app:app --port 8081
_start_service dispatcher \
  env SANDBOX_REPO_PATH="$REPO" RUN_STORE_DIR="$RUN_STORE_DIR" \
  uv run python -m orchestrator.orchestrator.dispatcher

# ── 8. Health checks ──────────────────────────────────────────────────────────
sep "Waiting for services"
for url in "$ORCH_URL/healthz" "$GW_URL/healthz"; do
  echo -n "  $url..."
  for i in $(seq 30); do
    if curl -sf "$url" > /dev/null 2>&1; then echo " $(_green 'OK')"; break; fi
    sleep 1
    if [ "$i" -eq 30 ]; then
      echo ""
      echo "  ERROR: $url did not respond. Check: tail -f $LOG_DIR/*.log" >&2
      exit 1
    fi
  done
done

# ── 9. Ready banner ───────────────────────────────────────────────────────────
echo ""
echo "  $(_dim '════════════════════════════════════════════════════════════')"
echo "  $(_bold '  Orchestra is running')"
echo "  $(_dim '════════════════════════════════════════════════════════════')"
echo "  $(_cyan '  Orchestrator') : $ORCH_URL"
echo "  $(_cyan '  Gateway')      : $GW_URL"
echo "  $(_cyan '  Dispatcher')   : watching Redis Streams for TASK_ASSIGNED events"
echo "  $(_dim '  Logs')         : $LOG_DIR/"
echo "  $(_dim '  Stop')         : make stop"
echo "  $(_dim '  Tail logs')    : make logs"
echo "  $(_dim '════════════════════════════════════════════════════════════')"

# ── 10. Task creation menu (skip if --spec / --plan passed on command line) ────
if [ -z "$SPEC" ] && [ -z "$PLAN" ]; then
  echo ""
  echo "  $(_bold 'How would you like to create project tasks?')"
  echo ""
  echo "  $(_cyan '1')  $(_bold 'No tasks yet') — start the platform only"
  echo "     $(_dim 'Come back with: uv run orchctl create-task  or  uv run python -m agents.planner.main')"
  echo ""
  echo "  $(_cyan '2')  $(_bold 'Generate from spec') — Orchestra calls the LLM to decompose your spec"
  echo "     $(_dim 'Provide a spec file (e.g. diary_spec.md); the planner creates tasks automatically.')"
  echo ""
  echo "  $(_cyan '3')  $(_bold 'Use the /arch-to-tasks skill') — generate a richer plan in Claude Code"
  echo "     $(_dim 'Run the skill first, review the JSON, then point this script at the output.')"
  echo "     $(_dim 'Best when you want to edit the plan before submitting, or use a stronger model.')"
  echo ""
  printf "  Choice [1/2/3]: "
  read -r _choice

  case "${_choice:-1}" in
    2)
      echo ""
      printf "  Spec file path (relative to $REPO, e.g. diary_spec.md): "
      read -r SPEC
      ;;
    3)
      echo ""
      echo "  $(_bold 'Step 1') — In Claude Code, run:"
      echo ""
      echo "    $(_cyan '/arch-to-tasks') <your-spec-or-architecture-file>"
      echo ""
      echo "  This analyses the document and writes $(_bold 'sandbox/sample-project/tasks.json')."
      echo "  You can open that file and edit it before submitting."
      echo ""
      printf "  $(_bold 'Step 2') — Path to tasks.json (or Enter to skip): "
      read -r _plan_path
      if [ -n "$_plan_path" ]; then
        PLAN="$_plan_path"
      else
        echo "  Skipping task submission. Run the planner manually when ready:"
        echo "    uv run python -m agents.planner.main --plan sandbox/sample-project/tasks.json"
      fi
      ;;
    *)
      echo ""
      echo "  $(_dim 'Platform is ready. No tasks created.')"
      echo "  $(_dim 'Use  uv run orchctl create-task  when you are ready to add tasks.')"
      ;;
  esac
fi

# ── 11. Submit tasks ──────────────────────────────────────────────────────────
if [ -n "$SPEC" ]; then
  echo ""
  sep "Generating task plan from spec  $(_dim "($SPEC)")"
  uv run python -m agents.planner.main \
    --spec "$SPEC" \
    --repo "$REPO" \
    --orchestrator-url "$ORCH_URL"
elif [ -n "$PLAN" ]; then
  echo ""
  sep "Submitting pre-built task plan  $(_dim "($PLAN)")"
  uv run python -m agents.planner.main \
    --plan "$PLAN" \
    --repo "$REPO" \
    --orchestrator-url "$ORCH_URL"
fi
