# CLAUDE.md — Orchestra (Human-Centric Multi-Agent Orchestration Platform)

## Session startup

At the start of every session, `scripts/check-services.sh` runs automatically via a
`UserPromptSubmit` hook (`.claude/settings.json`). It checks orchestrator, gateway, and
dispatcher in ~50ms and runs `scripts/start-session.sh` only if something is down.

If you notice services are down mid-session (e.g. a task fails with connection errors),
run `bash scripts/check-services.sh` or `bash scripts/start-session.sh` to recover.

Tasks that are `awaiting_human` due to an env_limitation (spend limit, auth failure) need:
1. Resolve the issue (raise spend limit at claude.ai/settings/usage, or `claude login`)
2. `curl -s -X POST http://localhost:8080/tasks/TASK-XXX/respond -H 'Content-Type: application/json' -d '{"response": "resolved", "actor": "human"}'`
3. Re-run the agent manually (dispatcher does not auto-resume awaiting_human tasks)

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
   failed/escalated/awaiting_human arms). Every transition writes an event and an audit
   row in one DB transaction. New validator-triggered paths: `completed → awaiting_human`
   (env_limitation detected) and `failed → awaiting_human` (dispatcher failsafe).
   Human-gate path: `owner=human` tasks skip subprocess dispatch, auto-advance to `running`,
   and transition via `orchctl human-done` (running→completed→validated) then `orchctl merge`
   (validated→merged→closed, no git merge). This unblocks DAG successors identically to
   agent tasks.
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
Agent memory system (Step 24 done): identity, episode, and skill memories persisted in
`agent_memories` Postgres table; injected into every context package; written by root agent,
dispatcher, and agents themselves via gateway.
Capability tokens (Step 25 done): HS256 JWT minted at run creation, verified by gateway;
write-scope enforcement on `write_artifact`; see ADR-006.
Provenance metadata (Step 26 done): `artifact_provenance` table; external content wrapped
in `<external-content>` delimiters in agent prompts.
Observability (Step 27 done): Prometheus + OTel traces on both services; Grafana dashboard;
Jaeger all-in-one; see ADR-007.
Policy file + Tier 2 hard gate (Step 28 done): `permissions/policy.yaml` auto-assigns task
tier from output path globs; `validated → merged` blocked for Tier 2 without
`details.tier2_override=True`; CLI `--tier-2-override` flag; see ADR-008.
Pluggable validator registry (Step 29 done): `permissions/validators.yaml` defines named
validators (file-exists, ruff, pytest, mypy, eslint, jest, llm-acceptance); tasks store
assigned validators; `create-task` prompts interactively to accept/edit auto-detected list;
`validate` displays per-check results; `GET /tasks/{id}/validation` exposes history;
`orchctl show` and `orchctl validator list` added.
Human-gate tasks as first-class DAG nodes (Step 30 done): planner creates `owner="human"`
tasks upfront; dispatcher skips subprocess and auto-advances to `running`; root agent writes
a manifest template (`human-gate/<slug>/manifest.json`) with empty key-value slots; human
fills the manifest and runs `orchctl human-done` to transition running→completed→validated;
`orchctl merge` closes the task without a git merge; downstream agents receive filled values
via context packager; `orchctl ask-task` answers manifest questions using task context;
`review` loop surfaces running human tasks with manifest status.

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
│   ├── shared/                # LLM client wrapper, agent base loop, cli_runner (backend config)
│   ├── root/                  # persistent root agent: accepts change requests, dispatches tasks
│   ├── planner/               # one-shot planner: spec -> tasks (plan_utils.py shared with root)
│   ├── worker/                # generic CLI worker wrapper (replaces claude_code/; configurable backend)
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
- Postgres tables: `tasks`, `events`, `runs`, `audit_rows`, `agent_memories`
  (`agent_memories` stores identity/episode/skill memories per `(agent_id, project_id, key)`).
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
  Observability (Phase 3): Jaeger UI 16686, OTLP HTTP 4318, Prometheus 9090, Grafana 3000.
- `REDIS_URL=redis://localhost:6380` — set in `.env`; used by `StreamPublisher` /
  `StreamConsumer` in `orchestrator/orchestrator/streams.py`.
- `OTLP_ENDPOINT=http://localhost:4318` — set in `.env` to activate distributed traces
  (Jaeger). Leave empty to skip tracing; Prometheus metrics are always active at `/metrics`.
- Postgres data is persisted at `~/.orchestra/pgdata` (WSL2 bind mount, not a named
  volume) to avoid the 128 MB Docker Desktop VHD limit.
- Agent backend config: `WORKER_BACKEND`, `PLANNER_BACKEND`, `VALIDATOR_BACKEND` env vars
  select which CLI/API backend each component uses. See `permissions/backends.yaml` for the
  registry; `orchctl config set worker-backend <name>` and `orchctl backend list` for UI.
  Legacy `AGENT_TYPE=claude-code|python` is still accepted as a fallback for `WORKER_BACKEND`.
  `owner` field on tasks is a domain identity (e.g. `backend-agent`, `devops-agent`, any
  string) -- it is NOT an execution backend selector. Any owner string is valid; a fresh
  identity probes its tools at first run and escalates to human if unavailable.

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
- `create-task TITLE [--owner AGENT_ID] [--accept CRITERION] [--input PATH] [--output PATH] [--depends-on TASK-ID]` — create a task manually; prompts interactively to accept/edit auto-detected validators; valid `--owner` values: `backend-agent`, `frontend-agent`, `qa-agent`, `devops-agent`, any custom identity string, or `human`
- `list [--status STATUS]` — list tasks
- `show TASK-ID` — full task detail: inputs, outputs, validators, acceptance criteria, and most recent validation result (works on closed tasks too)
- `approve TASK-ID` — advance through human approval gate (created→assigned, validated→merged)
- `run-task TASK-ID --repo PATH [--agent-id AGENT_ID]` — assemble context package and start run (assigned→running); `--agent-id` must match task owner for gateway auth
- `validate TASK-ID --repo PATH` — run all assigned validators on agent branch, display per-check table (completed→validated/failed)
- `merge TASK-ID --repo PATH` — merge agent branch into main via gateway, close task (validated→merged→closed); for `owner=human` tasks, skips git merge and closes the control-plane record only
- `human-done TASK-ID --repo PATH` — mark a human-gate task complete: reads `human-gate/<slug>/manifest.json`, validates all keys are filled, transitions running→completed→validated. Then run `orchctl merge` to close and unblock successors.
- `ask-task TASK-ID "question" --repo PATH [--model MODEL]` — one-shot LLM answer grounded in the task's acceptance criteria, manifest, and input files; use to get specific guidance on what format a value needs, where to find a setting, etc. Add `--session` (or omit the question) for a multi-turn interactive conversation that remembers prior answers within the session.
- `review --repo PATH` — interactive approval loop: auto-validates completed tasks, shows per-check results, prompts for merge; also surfaces running human-gate tasks with their manifest status
- `validator list` — show all validators in `permissions/validators.yaml` with name, auto-detect flag, and description
- `memory list [--agent AGENT_ID] [--type TYPE] [--project PROJECT]` — list agent memory rows (human safety valve)
- `memory show MEMORY_ID [--agent AGENT_ID]` — show full content of one memory row (accepts 8-char UUID prefix)
- `memory delete MEMORY_ID [--agent AGENT_ID] [--reason TEXT] [--yes]` — delete a memory row and write an audit record
- `identities [--agent AGENT-ID]` — list agent identity profiles with domain expertise, task history, and skill breakdown
- `teach AGENT-ID "fact" [--topic TOPIC]` — inject a human-taught skill into an agent's memory (key: `skill/human/{topic}`)
- `forget AGENT-ID TOPIC-OR-ID [--yes]` — remove a human-taught skill by topic slug or 8-char memory ID
- `ask AGENT-ID "question" [--model MODEL]` — one-shot competency probe; LLM backend set by `orchctl config set llm-backend <claude|python>`
- `session AGENT-ID [--model MODEL]` — multi-turn interactive identity REPL (`claude` backend: launches `claude` interactively; `python` backend: requires `ANTHROPIC_API_KEY`)
- `config show` — show current orchctl session config (LLM backend)
- `config set KEY VALUE` — set a session config value; `llm-backend` accepts `claude` or `python`; stored in `~/.config/orchestra/config`
- `doctor` — pre-flight check: verify platform services, configured backends, SANDBOX_REPO_PATH, and common domain tools; prints remediation hints for each failure

Orchestrator API (port 8080) — notable endpoints added in Phase 3:
- `GET /validators` — return the validator registry from `permissions/validators.yaml`
- `GET /tasks/{id}/validation` — return the most recent validation result (full check list) from the audit row; works after task is closed

Gateway service (port 8081) — start with `uvicorn gateway.gateway.app:app --port 8081`:
- `POST /read_artifact` — read a file from the managed repo (audited)
- `POST /write_artifact` — write a file to the managed repo (audited)
- `POST /run_command` — run a command in the repo (subprocess; Docker sandbox in Phase 3) (audited)
- `POST /emit_event` — write an event to the control plane (audited)
- `POST /git/branch` — create or checkout a branch (audited)
- `POST /git/commit` — stage paths and commit (audited)
- `POST /git/merge` — merge agent branch into target branch (requires validated status, audited)
- `POST /memory/upsert` — upsert an agent memory row (audited); agents may only write `memory_type="skill"`; platform writes (dispatcher, root-agent) use `X-Platform-Actor` header; content cap 2000 chars; skill deduplication merges same-topic rows
- `POST /memory/search` — keyword search over agent's own memories + shared pool (`agent_id="shared"`); derives agent_id from tasks.owner; audited

**Agent workers — two modes:**

1. **Python loop agents** (`backend-agent`, `frontend-agent`, `qa-agent`): custom Python loops
   that call the Anthropic API via `agents/shared/loop.py`. Require `ANTHROPIC_API_KEY` in `.env`.
   Active when `WORKER_BACKEND=python-api`.
   ```
   python -m agents.backend.main \
     --context /path/to/<run_id>.json \
     --run-id <uuid> \
     [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
   ```

2. **CLI worker agent** (all owner types, default): the generic `agents.worker.main` wrapper
   invokes any CLI-based backend (default: `claude`). Backend is configured via
   `WORKER_BACKEND` env var or `orchctl config set worker-backend <name>`. Does NOT need
   `ANTHROPIC_API_KEY` for the claude-code backend. Branch creation and git commit go through
   the gateway; individual file writes are not individually audited (Phase 3 revisit).
   ```
   python -m agents.worker.main \
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
