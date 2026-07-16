# CLAUDE.md — Orchestra (Human-Centric Multi-Agent Orchestration Platform)

## What this project is

An orchestration platform where a human owns intent, AI agents own execution, and the
orchestrator owns governance. Agents collaborate through shared state (a Git repo) and
persisted events, never through free-form chat. Every side effect flows through a tool
gateway that enforces task-scoped permissions and writes an audit record.

The authoritative design doc is `docs/design/orchestrator-mvp-v0.2.md`. Read it before
making architectural decisions. If a change contradicts it, stop and ask the human.

## Non-negotiable invariants

These hold from the first commit. Never violate them, even for a "quick test":

1. **Gateway-only side effects.** Agents never get raw Git credentials, direct DB access,
   or unmediated shell. All reads/writes/executions/event emissions go through the
   gateway service, which checks permissions and audits atomically with the action.
2. **Two planes.** Git is the artifact plane (docs, code, ADRs, reports). Postgres is the
   control plane (tasks, events, runs, capabilities, audit). Never store artifacts in
   Postgres or control state in Git.
3. **Append-only events.** The `events` table is never updated or deleted. Orchestrator
   state must be reconstructable by replaying events.
4. **Explicit state machine.** Task status changes only through defined transitions
   (created → assigned → running → completed → validated → merged → closed, plus
   failed/escalated arms). Every transition writes an event and an audit row in one
   DB transaction.
5. **Nothing merges to main of a managed project repo without the merge flow**
   (validator → review → merge via gateway). Tier rules come later; for now every merge
   is human-approved via `orchctl merge`.
6. **Provenance discipline.** Artifact metadata carries provenance (human/agent/external).
   External-provenance content is wrapped in delimiters when placed in prompts and never
   goes into system prompts.

## Current phase

**Phase 1 complete.** Walking skeleton shipped: task CRUD + CLI, context packager,
gateway, single backend agent loop, validator (ruff + pytest), human merge flow.
Retrospective: `docs/design/phase1-retro.md`.

**Phase 2 complete.** Redis Streams event bus, DAG scheduling, multi-agent fan-out, retry,
Tier 0 auto-merge, Claude Code as agent worker, interactive review loop, Phase 2 retro.
Retrospective: `docs/design/phase2-retro.md`.

**Phase 3 in progress.** Persistent root agent (Step 23 done): accepts change requests via
`orchctl request`, decomposes into tasks, dispatches sub-agents.

Phase gates and weekly breakdown are in the design doc, Part 5.

## Repository layout

```
orchestra/
├── CLAUDE.md                  # this file
├── pyproject.toml             # uv workspace root
├── docker-compose.yml         # postgres (+ redis from Phase 2)
├── Makefile                   # canonical commands; add new ones here
├── orchestrator/              # control plane: task state machine, DAG (later),
│   ├── orchestrator/          #   context packager, event log, scheduling
│   └── tests/
├── gateway/                   # tool gateway: permission checks, audited side effects,
│   ├── gateway/               #   sandboxed run_command (docker, no network)
│   └── tests/
├── agents/
│   ├── shared/                # LLM client wrapper (token/cost logging), agent base loop
│   ├── root/                  # persistent root agent: accepts change requests, dispatches tasks
│   ├── planner/               # one-shot planner: spec -> tasks (plan_utils.py shared with root)
│   └── backend/               # Phase 1 backend agent (prompt + config)
├── schemas/                   # JSON Schemas: Task, Event, AgentIdentity, RunRecord,
│                              #   Capability. Versioned via schema_version field.
├── cli/                       # orchctl: create-task, list, assign, approve, merge
├── infra/                     # alembic migrations, deployment scripts
├── docs/
│   ├── design/                # v0.1 and v0.2 design docs
│   └── adr/                   # ADR-001..N, never deleted
└── sandbox/sample-project/    # the managed demo repo agents operate on
```

## Tech stack and conventions

- Python 3.12, FastAPI, Pydantic v2 (models generated/hand-written from `schemas/`),
  SQLAlchemy 2.x + Alembic, Postgres 16. Redis only from Phase 2.
- Package management: `uv`. Lint/format: `ruff` (line length 100). Tests: `pytest`.
- Typing is mandatory on public functions. `ruff check` and `pytest` must pass before
  any commit is considered done.
- LLM calls only through `agents/shared/llm.py` (single client wrapper that records
  tokens and cost per call into the control plane). Never call a provider SDK directly
  elsewhere.
- Commit messages: `[TASK-ID] imperative summary` when work maps to a platform task,
  conventional `feat:/fix:/chore:` otherwise. No em dashes in docs or messages.
- Secrets via `.env` (gitignored); `.env.example` documents every variable. Never
  hardcode credentials, account IDs, or API keys.
- The developer works on Windows + WSL2. Everything must run inside WSL2/Docker;
  do not assume Docker Desktop paths. Ports: Postgres 5433 on host (5432 is often
  taken), gateway 8081, orchestrator 8080, Redis 6380 (host) mapped from container 6379.
- `REDIS_URL=redis://localhost:6380` — set in `.env`; used by `StreamPublisher` /
  `StreamConsumer` in `orchestrator/orchestrator/streams.py`.
- Postgres data is persisted at `~/.orchestra/pgdata` (WSL2 bind mount, not a named
  volume) to avoid the 128 MB Docker Desktop VHD limit.

## Commands

All canonical commands live in the Makefile. Current targets:

- `make up` / `make down` — docker compose stack
- `make migrate` — alembic upgrade head
- `make clean-db` — tear down Postgres volume and re-migrate (fixes disk-full errors)
- `make test` — pytest across all packages
- `make lint` — ruff check + format --check
- `make demo` — run the Phase 1 end-to-end demo (`scripts/demo.sh`; requires both services running and `ANTHROPIC_API_KEY`)
- `make demo-v2` — run the Phase 2 three-task fan-out demo (`scripts/demo_v2.sh`)
- `make root-agent` — start the root agent standalone (SANDBOX_REPO_PATH and AGENT_TYPE must be set)

`orchctl` commands (run via `uv run orchctl`):
- `request "description" [--spec PATH]` — submit a change request to the root agent; the root agent decomposes it into tasks and dispatches agents automatically
- `create-task TITLE [--owner AGENT_ID] [--accept CRITERION] [--input PATH] [--output PATH] [--depends-on TASK-ID]` — create a task manually; valid `--owner` values: `backend-agent`, `frontend-agent`, `qa-agent`, `claude-code-agent`
- `list [--status STATUS]` — list tasks
- `approve TASK-ID` — advance through human approval gate (created→assigned, validated→merged)
- `run-task TASK-ID --repo PATH` — assemble context package and start run (assigned→running)
- `validate TASK-ID --repo PATH` — run validator (ruff + pytest) on agent branch (completed→validated/failed)
- `merge TASK-ID --repo PATH` — merge agent branch into main via gateway, close task (validated→merged→closed)
- `review --repo PATH` — interactive approval loop: auto-validates completed tasks, shows ruff/pytest results, prompts for merge

Gateway service (port 8081) — start with `uvicorn gateway.gateway.app:app --port 8081`:
- `POST /read_artifact` — read a file from the managed repo (audited)
- `POST /write_artifact` — write a file to the managed repo (audited)
- `POST /run_command` — run a command in the repo (subprocess; Docker sandbox in Phase 3) (audited)
- `POST /emit_event` — write an event to the control plane (audited)
- `POST /git/branch` — create or checkout a branch (audited)
- `POST /git/commit` — stage paths and commit (audited)
- `POST /git/merge` — merge agent branch into target branch (requires validated status, audited)

**Agent workers — two modes:**

1. **Python loop agents** (`backend-agent`, `frontend-agent`, `qa-agent`): custom Python loops
   that call the Anthropic API via `agents/shared/loop.py`. Require `ANTHROPIC_API_KEY` in `.env`.
   ```
   python -m agents.backend.main \
     --context /path/to/<run_id>.json \
     --run-id <uuid> \
     [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
   ```

2. **Claude Code agent** (`claude-code-agent`): launches the `claude` CLI as a subprocess.
   Requires the `claude` CLI to be installed and authenticated (`claude login`). Does NOT
   need `ANTHROPIC_API_KEY`. Branch creation and git commit still go through the gateway;
   individual file writes are not individually audited (Phase 3 revisit).
   ```
   python -m agents.claude_code.main \
     --context /path/to/<run_id>.json \
     --run-id <uuid> \
     [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
   ```

All agent workers: defaults are `--repo $SANDBOX_REPO_PATH`, `--gateway-url http://localhost:8081`,
`--orchestrator-url http://localhost:8080`. Exit 0 on success, 1 on failure. The dispatcher
launches agents automatically; manual invocation is for debugging only.

If you add a workflow, add a Make target for it and document it here.

## Definition of done (per task)

1. Code + tests written; `make lint` and `make test` pass.
2. New/changed DB schema has an alembic migration.
3. Any architectural decision recorded as a new ADR in `docs/adr/` (one page max).
4. Every state transition and gateway operation touched by the change writes correct
   audit rows (assert this in tests, not by inspection).
5. CLAUDE.md updated if commands, layout, or invariants changed.

## How to work in this repo

- Prefer small vertical slices that keep `make demo` working over broad horizontal
  refactors.
- When the design doc and existing code disagree, the design doc wins unless an ADR
  says otherwise; if neither covers it, write the ADR first, then the code.
- Ask the human before: adding a dependency, changing a schema in `schemas/`,
  touching the state machine transitions, or expanding Phase scope.
- Do not mock the gateway inside agent code to "move faster". The gateway boundary
  is the product.
