#!/usr/bin/env bash
# Orchestra setup script — takes a fresh clone to a fully running platform.
#
# Usage:
#   bash scripts/setup.sh                        # start services only
#   bash scripts/setup.sh --spec diary_spec.md   # start + submit tasks from spec
#
# The script is idempotent: re-running it skips steps that are already done
# (services already running, .env already exists, sandbox already initialised).

set -euo pipefail

# ── Parse args ────────────────────────────────────────────────────────────────
SPEC=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --spec) SPEC="$2"; shift 2 ;;
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

sep() { echo ""; echo "──── $* ────"; }

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
sep "Checking prerequisites"

for cmd in python3 uv docker git curl; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: '$cmd' not found. Please install it and re-run." >&2
    exit 1
  fi
done

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"; then
  echo "Python $PY_VER: OK"
else
  echo "ERROR: Python 3.12+ required (found $PY_VER)." >&2
  exit 1
fi

# ── 2. Python packages ────────────────────────────────────────────────────────
sep "Installing Python packages"
cd "$ROOT"
uv sync --quiet
echo "uv sync: OK"

# ── 3. .env bootstrap ─────────────────────────────────────────────────────────
sep "Configuring .env"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example"
fi

# Check if API key is missing or blank in the file
if ! grep -qE "^ANTHROPIC_API_KEY=.+" "$ROOT/.env"; then
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    # Already set in the environment; write it into the file
    sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}|" "$ROOT/.env"
    echo "ANTHROPIC_API_KEY: written from environment"
  else
    echo ""
    echo "ANTHROPIC_API_KEY is not set."
    read -rp "Enter your Anthropic API key (sk-ant-...): " _key
    sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${_key}|" "$ROOT/.env"
    echo "ANTHROPIC_API_KEY: saved to .env"
  fi
fi

# Source .env so all subsequent commands see the variables
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
echo ".env: loaded"

# ── 4. Docker infrastructure ───────────────────────────────────────────────────
sep "Starting Docker services (Postgres + Redis)"

make -C "$ROOT" up

# Wait for Postgres
echo -n "Waiting for Postgres..."
for i in $(seq 30); do
  if docker exec orchestra-postgres-1 pg_isready -U orchestra -q 2>/dev/null; then
    echo " ready"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then echo ""; echo "ERROR: Postgres did not become ready." >&2; exit 1; fi
done

# Wait for Redis
echo -n "Waiting for Redis..."
for i in $(seq 30); do
  if docker exec orchestra-redis-1 redis-cli ping 2>/dev/null | grep -q "PONG"; then
    echo " ready"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then echo ""; echo "ERROR: Redis did not become ready." >&2; exit 1; fi
done

# ── 5. Migrations ─────────────────────────────────────────────────────────────
sep "Applying database migrations"
make -C "$ROOT" migrate
echo "Migrations: OK"

# ── 6. Sandbox git repo ───────────────────────────────────────────────────────
sep "Initialising sandbox project repo"

if [ ! -d "$REPO/.git" ]; then
  cd "$REPO"
  git init -b main
  git config user.email "demo@orchestra"
  git config user.name "Orchestra Demo"
  git add .
  git commit -m "chore: initial sample project"
  cd "$ROOT"
  echo "Sandbox repo: initialised at $REPO"
else
  echo "Sandbox repo: already initialised"
fi

# ── 7. Background services ────────────────────────────────────────────────────
sep "Starting platform services"

_start_service() {
  local name="$1"; shift
  local pid_file="$PID_DIR/$name.pid"

  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name: already running (pid $(cat "$pid_file"))"
    return
  fi

  # Remove stale PID file if process is dead
  rm -f "$pid_file"

  "$@" >> "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$pid_file"
  echo "$name: started (pid $!, log: $LOG_DIR/$name.log)"
}

cd "$ROOT"

_start_service orchestrator \
  uv run uvicorn orchestrator.orchestrator.api:app --port 8080

_start_service gateway \
  uv run uvicorn gateway.gateway.app:app --port 8081

_start_service dispatcher \
  env SANDBOX_REPO_PATH="$REPO" RUN_STORE_DIR="$RUN_STORE_DIR" \
  uv run python -m orchestrator.orchestrator.dispatcher

# ── 8. Health checks ──────────────────────────────────────────────────────────
sep "Waiting for services to be ready"

for url in "$ORCH_URL/healthz" "$GW_URL/healthz"; do
  echo -n "Checking $url..."
  for i in $(seq 30); do
    if curl -sf "$url" > /dev/null 2>&1; then
      echo " OK"
      break
    fi
    sleep 1
    if [ "$i" -eq 30 ]; then
      echo ""
      echo "ERROR: $url did not respond within 30s." >&2
      echo "  Check logs: tail -f $LOG_DIR/*.log" >&2
      exit 1
    fi
  done
done

# ── 9. Status summary ─────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo " Orchestra is running"
echo "════════════════════════════════════════"
echo " Orchestrator : $ORCH_URL"
echo " Gateway      : $GW_URL"
echo " Logs         : $LOG_DIR/"
echo " PIDs         : $PID_DIR/"
echo ""
echo " To stop      : make stop"
echo " To tail logs : make logs"
echo "════════════════════════════════════════"

# ── 10. Planner (if --spec given) ─────────────────────────────────────────────
if [ -n "$SPEC" ]; then
  echo ""
  sep "Running planner — decomposing $SPEC into tasks"
  uv run python -m agents.planner.main \
    --spec "$SPEC" \
    --repo "$REPO" \
    --orchestrator-url "$ORCH_URL"
fi
