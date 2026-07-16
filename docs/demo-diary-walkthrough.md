# Demo walkthrough: Caesar Cipher Diary

This guide walks a first-time user through running the Orchestra Phase 2 demo end-to-end
using a real project specification: a small diary web app where entries are stored encrypted
with a user-chosen Caesar cipher shift.

Three AI agents collaborate: a backend agent implements the API and cipher logic, then (in
parallel) a frontend agent builds the single-page UI and a QA agent writes the test report.
The platform governs every step — task assignment, event fan-out, validation, and merge — while
you retain approval at each human gate.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12+ | Check with `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | any recent | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker | 24+ | Must be running; on Windows use WSL2 Docker Engine |
| Git | any | Pre-installed on most systems |
| `ANTHROPIC_API_KEY` | — | From console.anthropic.com |

> **Windows users:** run every command inside a WSL2 terminal, not PowerShell or CMD.

---

## Quick start (recommended)

If you just want to run the demo, one command does everything:

```bash
git clone <repo-url> orchestra && cd orchestra
bash scripts/setup.sh --spec diary_spec.md
```

`setup.sh` installs packages, bootstraps `.env` (prompting for your API key if needed),
starts Postgres + Redis, runs migrations, initialises the sandbox repo, starts the
orchestrator / gateway / dispatcher in the background, and then runs the planner agent
which reads `diary_spec.md` and submits the three tasks automatically.

After it completes, skip straight to **step 7** (validate and merge the backend task) once
the backend agent finishes. Use `uv run orchctl list` to watch progress and `make logs` to
tail the service logs.

To stop all background services: `make stop`

---

## Detailed walkthrough

The sections below explain each step individually — useful for understanding what the
platform is doing or for troubleshooting.

---

## 1. Initial setup

```bash
# Clone the repo (skip if already done)
git clone <repo-url> orchestra
cd orchestra

# Install all Python packages
uv sync

# Create your .env from the example and add your key
cp .env.example .env
# Open .env and set:  ANTHROPIC_API_KEY=sk-ant-...
```

---

## 2. Start the infrastructure

```bash
make up        # starts Postgres (port 5433) and Redis (port 6380) in Docker
make migrate   # creates all control-plane tables
```

Verify both containers are healthy:

```bash
docker compose ps
# Both services should show "healthy" or "running"
```

---

## 3. Start the platform services

You need three long-running processes. Open **four terminal windows** (all from the `orchestra/`
directory).

**Terminal 1 — Orchestrator (task state machine + API)**

```bash
uv run uvicorn orchestrator.orchestrator.api:app --port 8080 --reload
```

**Terminal 2 — Gateway (audited tool execution)**

```bash
uv run uvicorn gateway.gateway.app:app --port 8081 --reload
```

**Terminal 3 — Dispatcher (event-driven agent launcher)**

```bash
SANDBOX_REPO_PATH=./sandbox/sample-project \
RUN_STORE_DIR=/tmp/orchestra/runs \
uv run python -m orchestrator.orchestrator.dispatcher
```

Keep terminal 4 free for running commands.

Quick sanity check:

```bash
curl -s http://localhost:8080/healthz   # should return {"status":"ok"}
curl -s http://localhost:8081/healthz   # should return {"status":"ok"}
```

---

## 4. Initialise the sample project repo

The agents commit their work to a local Git repo at `sandbox/sample-project`.
Run this once (or whenever you want a clean slate):

```bash
cd sandbox/sample-project
git init -b main
git config user.email "demo@orchestra"
git config user.name "Orchestra Demo"
git add .
git commit -m "chore: initial sample project"
cd ../..
```

---

## 5. Create the three diary tasks

The project specification lives at `sandbox/sample-project/diary_spec.md`.
The backend task reads it and produces code; the frontend and QA tasks run in parallel after
the backend task completes.

```bash
# Task 1: Backend — implement the API, cipher logic, and tests
BE_OUT=$(uv run orchctl create-task \
  "Implement Caesar diary API (register, login, entries CRUD, cipher)" \
  --owner  backend-agent \
  --input  "diary_spec.md" \
  --output "app/main.py" \
  --output "app/cipher.py" \
  --output "tests/test_app.py" \
  --accept "All 15 tests pass under pytest" \
  --accept "ruff check . passes" \
  --risk-tier 1)
echo "$BE_OUT"
BE_ID=$(echo "$BE_OUT" | grep "^Created" | awk '{print $2}' | tr -d ':')
echo "Backend task: $BE_ID"

# Task 2: Frontend — single-page diary UI (waits for backend to finish)
FE_OUT=$(uv run orchctl create-task \
  "Build single-page diary frontend (login, entry list, decrypt)" \
  --owner      frontend-agent \
  --depends-on "$BE_ID" \
  --input      "diary_spec.md" \
  --output     "frontend/index.html" \
  --accept     "Login, register, add entry, and decrypt all work in the browser" \
  --risk-tier  1)
FE_ID=$(echo "$FE_OUT" | grep "^Created" | awk '{print $2}' | tr -d ':')
echo "Frontend task: $FE_ID"

# Task 3: QA — test report (also waits for backend, runs alongside frontend)
QA_OUT=$(uv run orchctl create-task \
  "QA review of Caesar diary implementation" \
  --owner      qa-agent \
  --depends-on "$BE_ID" \
  --input      "diary_spec.md" \
  --output     "qa_report.md" \
  --accept     "qa_report.md covers all 15 test cases and cipher edge cases" \
  --risk-tier  1)
QA_ID=$(echo "$QA_OUT" | grep "^Created" | awk '{print $2}' | tr -d ':')
echo "QA task: $QA_ID"
```

Confirm all three tasks exist:

```bash
uv run orchctl list
```

---

## 6. Approve and run the backend task

Tasks start in `created` status. You must approve each task before it runs — this is the
first human gate. Since only the backend task has no dependencies, it is the only one
eligible now.

```bash
# Human gate 1: created → assigned
uv run orchctl approve "$BE_ID"

# Start the run: assembles context package, transitions to 'running'
uv run orchctl run-task "$BE_ID" \
  --repo     ./sandbox/sample-project \
  --agent-id backend-agent
```

`run-task` prints a `run_id` and a path to the context package JSON. The dispatcher picks up
the `TASK_ASSIGNED` event and launches the backend agent automatically (you should see activity
in Terminal 3). If you want to launch the agent manually instead:

```bash
uv run python -m agents.backend.main \
  --context <path/to/context.json> \
  --run-id  <run_id> \
  --repo    ./sandbox/sample-project
```

The backend agent will read `diary_spec.md`, implement `app/main.py`, `app/cipher.py`, and
`tests/test_app.py`, commit them to a branch named `agent/backend/$BE_ID`, then signal
completion.

---

## 7. Validate and merge the backend task

Once the backend agent finishes the task moves to `completed`. Run the validator:

```bash
uv run orchctl validate "$BE_ID" --repo ./sandbox/sample-project
# Runs ruff check + pytest on the agent's branch.
# On success: status → 'validated'
```

Then open the second human gate (validated → merged) and merge to main:

```bash
uv run orchctl approve "$BE_ID"       # human approves the validated output
uv run orchctl merge   "$BE_ID" --repo ./sandbox/sample-project
# Merges agent/backend/$BE_ID → main via the gateway (audited).
# Status: validated → merged → closed
```

---

## 8. Watch the fan-out

When the backend task transitions to `completed`, the dispatcher detects that the frontend
and QA tasks are now unblocked (their only dependency is done). It automatically transitions
both to `assigned` and emits `TASK_ASSIGNED` events.

You can watch this happen in Terminal 3, or poll from Terminal 4:

```bash
watch -n 3 "uv run orchctl list"
```

Once both show `assigned` (or `running`), the dispatcher has already launched both agents.

---

## 9. Validate and merge the frontend and QA tasks

After both agents finish (status `completed`), validate and merge each:

```bash
# Frontend
uv run orchctl validate "$FE_ID" --repo ./sandbox/sample-project
uv run orchctl approve  "$FE_ID"
uv run orchctl merge    "$FE_ID" --repo ./sandbox/sample-project

# QA
uv run orchctl validate "$QA_ID" --repo ./sandbox/sample-project
uv run orchctl approve  "$QA_ID"
uv run orchctl merge    "$QA_ID" --repo ./sandbox/sample-project
```

---

## 10. Inspect the results

```bash
# All three tasks should be 'closed'
uv run orchctl list

# Three merge commits on main
git -C sandbox/sample-project log --oneline

# Run the diary app locally
cd sandbox/sample-project
uv run uvicorn app.main:app --port 8000 --reload
# Open http://localhost:8000 in your browser
```

You should see the diary login page. Register a username and 5-character PIN, write your first
entry with a shift of your choice, then click Decrypt to read it back.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `curl /healthz` times out | Service not started | Check the relevant terminal for errors |
| `TASK_ASSIGNED` never arrives | Dispatcher not running | Start Terminal 3 |
| Agent exits immediately with an error | `ANTHROPIC_API_KEY` missing or invalid | Check `.env` |
| `git checkout` fails in validator | Agent branch was not committed | Re-run the agent |
| Postgres connection refused | Container not healthy | `make down && make up && make migrate` |
| Disk-full error from Postgres | VHD limit hit | `make clean-db` (wipes data, re-migrates) |
| Frontend or QA tasks stuck in `created` | Backend task not yet `completed` | Wait for backend agent to finish |

---

## Quick reference

```
make up           start Postgres + Redis
make migrate      create/update DB schema
make test         run the unit + integration test suite
make lint         ruff check + format check
make demo-v2      automated three-task fan-out demo (no diary spec; uses script defaults)

orchctl create-task TITLE [options]   create a task
orchctl list                          list all tasks
orchctl approve  TASK-ID              advance through the current human gate
orchctl run-task TASK-ID --repo PATH  start an agent run
orchctl validate TASK-ID --repo PATH  run ruff + pytest on the agent branch
orchctl merge    TASK-ID --repo PATH  merge agent branch to main and close task
```
