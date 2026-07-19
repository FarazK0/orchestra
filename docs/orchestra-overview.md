# Orchestra — Platform Overview

**Human-centric multi-agent orchestration platform.**
Humans own intent. Agents own execution. The orchestrator owns governance.

---

## The core idea

Most multi-agent frameworks are conversation-first: agents talk to each other in a loop
and the plan emerges from the chat. Orchestra inverts this. A human (or a root agent acting
on a human's behalf) authors the plan as an explicit task graph. Agents are workers attached
to nodes of that graph. They never talk to each other directly; they collaborate through
shared state in a Git repository and a Postgres event log.

The mental model is a software engineering organisation, not a swarm:

- **Human** — product owner; defines intent, approves merges, resolves escalations
- **Root agent** — receives change requests in plain English, decomposes them into tasks
- **Specialist agents** — backend / frontend / QA workers; each owns one responsibility
- **Orchestrator** — pure governance; holds the DAG, dispatches work, enforces policy
- **Gateway** — the only path to side effects; every file write, git commit, and command runs here
- **Validator** — ruff + pytest gate before any output touches `main`

---

## Two-plane architecture

```
┌─────────────────────────────────────────────────────────┐
│  ARTIFACT PLANE  (Git)                                  │
│  sandbox/sample-project/                                │
│  Agent branches:  agent/backend/TASK-001                │
│  Merge target:    main                                  │
│  Content:         code, tests, specs, ADRs, reports     │
└──────────────────────────┬──────────────────────────────┘
                           │  gateway mediates all reads/writes
┌──────────────────────────┴──────────────────────────────┐
│  CONTROL PLANE  (Postgres + Redis)                      │
│  Tables:  tasks, events, runs, audit, agent_memories,      │
│           artifact_provenance                               │
│  Bus:     Redis Streams  orchestra:events               │
│  State:   task DAG, lifecycle, memory, audit log        │
└─────────────────────────────────────────────────────────┘
```

**Invariant:** artifacts never go in Postgres; control state never goes in Git.
Violating this invariant collapses the clean separation that makes the system auditable.

---

## Components

### Orchestrator (`orchestrator/`)

FastAPI service on port 8080. Holds the task state machine, event log, context packager,
DAG scheduler, and the Alembic-managed Postgres schema.

**Task state machine:**

```
created → assigned → running → completed → validated → merged → closed
                                     ↘ failed → (retry N times) → escalated
```

Every transition writes an event row and an audit row in one DB transaction.
State is fully reconstructable by replaying the `events` table from the beginning.

**DAG scheduler:** tasks with `depends_on` only advance to `assigned` once all
upstream tasks reach `closed`. The dispatcher auto-assigns root tasks (no
dependencies) immediately.

**Context packager:** before a task runs, assembles a context package — task spec,
acceptance criteria, input artifact content read from the Git repo, and the agent's
memory snapshot. The exact package is saved with the run record so every run is
reproducible.

### Gateway (`gateway/`)

FastAPI service on port 8081. The only component that touches the filesystem and Git.
Every agent action flows through it.

**Endpoints:**

| Endpoint | What it does |
|---|---|
| `POST /read_artifact` | Read a file from the managed repo |
| `POST /write_artifact` | Write a file to the managed repo |
| `POST /run_command` | Run a shell command in the repo (subprocess; Docker sandbox in Phase 3) |
| `POST /emit_event` | Write an event to the control plane |
| `POST /git/branch` | Create or checkout an agent branch |
| `POST /git/commit` | Stage paths and commit |
| `POST /git/merge` | Merge an agent branch into `main` (requires `validated` status) |
| `POST /memory/upsert` | Write or update an agent memory row |
| `POST /memory/search` | Keyword search over the agent's memories and the shared pool |

Every call writes an `audit_rows` record atomically. An agent that bypasses the gateway
has no path to side effects — this is enforced structurally, not by policy.

### Event bus (Redis Streams)

One stream: `orchestra:events`. Consumer groups per agent type.
At-least-once delivery with `event_id` dedup table for idempotent consumption.
Pending-entry reclaim on restart recovers from mid-run crashes.

Key event types: `TASK_ASSIGNED`, `TASK_COMPLETED`, `TASK_VALIDATED`, `TASK_FAILED`,
`TASK_MERGED`, `TASK_ESCALATED`, `CHANGE_REQUEST`.

### Root agent (`agents/root/`)

Persistent daemon. Subscribes to the `root:requests` Redis stream.
When a `CHANGE_REQUEST` event arrives (from `orchctl request "..."`), it:

1. Takes a snapshot of the managed repo (file tree + git log)
2. Fetches existing tasks to avoid re-creating completed work
3. Calls the planner (claude CLI or LLM) with `CHANGE_REQUEST_SYSTEM_PROMPT` to decompose the request into tasks with specialist owners
4. Creates tasks in the orchestrator
5. Auto-assigns root tasks (no `depends_on`) so the dispatcher picks them up immediately
6. Seeds identity memory for each specialist agent type in the plan
7. Writes shared project conventions into the shared memory pool

### Dispatcher (`orchestrator/orchestrator/dispatcher.py`)

Event-driven loop that reacts to Redis stream events:

- `TASK_ASSIGNED` → create run, assemble context package, launch agent subprocess
- `TASK_COMPLETED` / `TASK_MERGED` → advance DAG successors to `assigned`
- `TASK_VALIDATED` → auto-merge Tier 0 tasks; advance successors
- `TASK_FAILED` → retry within budget or escalate

**Agent routing:** reads `task.owner` to determine which module to launch.
When `AGENT_TYPE=claude-code` (the default), all specialist owners
(`backend-agent`, `frontend-agent`, `qa-agent`) run via `agents.claude_code.main`.
When `AGENT_TYPE=python`, each owner runs its dedicated Python loop module.

After each task completes the dispatcher writes an episode memory under `task.owner`
summarising files written, commands run, and the commit SHA.

### Agent workers

**Two execution substrates, four specialist identities.**

The `AGENT_TYPE` env var controls the binary. The `task.owner` field controls the persona.

| Owner | Responsibility |
|---|---|
| `backend-agent` | APIs, data models, business logic, migrations, server-side tests |
| `frontend-agent` | HTML, CSS, JavaScript, single-page UI, templates |
| `qa-agent` | Test plans, QA reports, risk assessment — no new features |
| `claude-code-agent` | Cross-cutting work that genuinely spans all layers |

**Claude Code agent** (`agents/claude_code/main.py`): launches `claude --dangerously-skip-permissions -p`
as a subprocess. No `ANTHROPIC_API_KEY` required — uses the claude CLI's own session auth.
Branch creation and commit still go through the gateway (audited).
Individual file writes are not individually audited (Phase 3 revisit).

**Python loop agents** (`agents/backend/`, `agents/frontend/`, `agents/qa/`): custom
loops that call the Anthropic API directly via `agents/shared/llm.py`. Require `ANTHROPIC_API_KEY`.

### Validator (`orchestrator/orchestrator/validator.py`)

Runs `ruff check` and `pytest` on the agent branch. Result is written as an event.
**Tier 0 auto-merge:** tasks that pass validation with no human-authored file
modifications are merged automatically; the human is notified but not blocked.

---

## Agent memory system

Three-layer persistent memory stored in the `agent_memories` Postgres table,
keyed on `(agent_id, project_id, key)`.

```
agent_memories
  agent_id      e.g. "backend-agent", "shared"
  project_id    "default" for now
  memory_type   identity | episode | skill | convention
  key           unique slug within (agent_id, project_id)
  content       up to 2000 chars
  last_used_at  updated on read for recency-ordered retrieval
```

**Identity** — who the agent is and its role in the project. Seeded by the root agent
on every change request (refreshed when 10+ tasks have completed since last seed).
One row per specialist: `(backend-agent, default, identity)`.

**Episode** — what happened on past tasks. Written by the dispatcher after each task
completes. Summarises files written, commands run, and the commit SHA.
One row per task: `(backend-agent, default, episode/TASK-001)`.

**Skill** — reusable patterns the agent discovered mid-task (project conventions,
preferred libraries, gotchas). Written by the agent itself via `POST /memory/upsert`.
The gateway enforces that agents may only write `memory_type="skill"`;
identity and episode are platform-written (`X-Platform-Actor` header required).

**Convention (shared pool)** — project-wide rules injected into every agent's context
regardless of type. Stored under `agent_id="shared"`.

**Memory injection:** the context packager queries `agent_id = task.owner` and injects
memory into the context package in four sections: identity, past episodes (top 10 by
recency), acquired skills (top 15), and shared conventions (top 10).

**Runtime search:** agents can search their own memory archive mid-task via
`POST /gateway/memory/search` with a keyword query. Results from the shared pool
are included automatically.

---

## Security model (current state)

**What is enforced now:**

- Agents have no direct filesystem access, Git credentials, or DB access.
  All effects flow through the gateway (structural enforcement, not policy).
- **Capability tokens (Phase 3):** the orchestrator mints a signed HS256 JWT at run
  creation, scoped to the task's `write_scope` and expiring at task deadline
  (`wall_clock_min + 30 min grace`, capped at 24 h). Agents pass it as
  `Authorization: Bearer` on every gateway call. The gateway verifies signature and
  expiry before the DB check. Active when `CAPABILITY_SECRET` is set; falls back to
  DB-only auth if not configured. See ADR-006.
- **Write-scope enforcement:** `write_artifact` rejects paths outside the token's
  `write_scope` list, so an agent cannot write outside its declared `task.outputs`.
- The gateway validates `(agent_id, task_id)` against active Run rows (DB check,
  retained alongside tokens as belt-and-suspenders).
- Memory writes are type-guarded: agents may only write `skill` memories.
  Platform writes (root agent, dispatcher) require `X-Platform-Actor` header.
- Branch isolation: every agent works on `agent/{role}/{task_id}`; nothing writes
  `main` directly. Merges require `validated` status.
- Append-only events: the `events` table is never updated or deleted.

**What is next (Phase 3/4 in queue):**

- Token revocation: Redis-based blocklist to immediately invalidate tokens when a task
  is cancelled, eliminating the current grace-period window (Phase 4).

---

## Repository layout

```
orchestra/
├── orchestrator/          control plane: task state machine, DAG, context packager,
│   └── orchestrator/        event log, dispatcher, validator, Alembic migrations
├── gateway/               tool gateway: permission checks, audited side effects
├── agents/
│   ├── shared/            LLM client wrapper (token + cost logging), agent base loop
│   ├── root/              persistent root agent: change requests → tasks
│   ├── planner/           one-shot planner: spec/plan JSON → tasks; shared plan_utils
│   ├── backend/           Python loop: backend specialist
│   ├── frontend/          Python loop: frontend specialist
│   ├── qa/                Python loop: QA specialist
│   └── claude_code/       claude CLI wrapper: any specialist via AGENT_TYPE
├── schemas/               JSON Schemas: Task, Event, AgentIdentity, RunRecord, Capability
├── cli/                   orchctl: request, create-task, list, approve, run-task,
│                            validate, merge, review, memory list/show/delete, cancel
├── infra/                 Alembic migrations (001-007)
├── scripts/
│   ├── setup.sh           one-command onboarding: starts all services, optional spec
│   └── demo_v2.sh         Phase 2 three-task fan-out demo
├── docs/
│   ├── design/            design docs and retros
│   └── adr/               ADR-001 to ADR-006 (never deleted)
└── sandbox/sample-project managed demo repo agents operate on
```

---

## Ports and infrastructure

| Service | Port | Notes |
|---|---|---|
| Orchestrator | 8080 | FastAPI; `GET /healthz`, `POST /tasks`, `GET /tasks`, state machine transitions |
| Gateway | 8081 | FastAPI; all audited side effects |
| Postgres | 5433 (host) | Data at `~/.orchestra/pgdata`; 5432 often occupied |
| Redis | 6380 (host) | Container port 6379; Streams + dedup |

---

## CLI reference (`orchctl`)

```
request "description" [--spec PATH]   submit a change request to the root agent
create-task TITLE [options]           create a task manually
list [--status STATUS]                list tasks
approve TASK-ID                       advance through human approval gate
run-task TASK-ID --repo PATH          assemble context package and start run
validate TASK-ID --repo PATH          run ruff + pytest on agent branch
merge TASK-ID --repo PATH             merge validated branch into main
review --repo PATH                    interactive approval loop
cancel TASK-ID                        cancel any non-terminal task
memory list [--agent] [--type]        list memory rows
memory show MEMORY_ID                 show full content of one row
memory delete MEMORY_ID [--yes]       delete and write audit record
```

---

## Build status by phase

### Phase 1 — Walking skeleton (complete)

Task CRUD, CLI (`orchctl create-task`, `list`, `approve`), context packager, gateway
with read/write/run/emit/git endpoints, single backend agent loop, validator (ruff +
pytest), human merge flow. Full audit trail. End-to-end demo working.

### Phase 2 — Concurrency (complete)

Redis Streams event bus, DAG scheduling with dependency gating, multi-agent fan-out
(backend/frontend/QA), retry with fresh-branch semantics, Tier 0 auto-merge,
interactive review loop (`orchctl review`), Claude Code as agent worker,
setup script + one-shot planner.

Post-phase additions: `orchctl cancel`, `cancelled` state, sandbox reset tooling.

### Phase 3 — Governance (in progress)

**Done:**

- **Step 23 — Persistent root agent:** `orchctl request` + Redis stream + planner
  decomposition; root tasks auto-assigned immediately.
- **Step 24 — Agent memory system v1:** identity/episode/skill memories in Postgres,
  injected into every context package; `orchctl memory` commands.
- **Step 24 v2 — Memory improvements:** top-K recency retrieval (context explosion
  prevention), shared pool (`agent_id="shared"`, `memory_type="convention"`), runtime
  keyword search (`POST /memory/search`), skill deduplication on write.
- **Specialist routing fix:** specialist owners (`backend-agent`, `frontend-agent`,
  `qa-agent`) are always assigned by the planner regardless of execution substrate.
  When `AGENT_TYPE=claude-code`, the dispatcher routes all of them to the claude CLI
  while preserving their specialist identity, memories, and episode accumulation.
- **Capability tokens (Step 25):** orchestrator mints HS256 JWT at run creation,
  embedded in the context package as `capability_token`. Gateway verifies signature
  and expiry before the DB check. Write-scope enforcement on `write_artifact` restricts
  paths to `task.outputs`. Opt-in via `CAPABILITY_SECRET` env var; backwards-compatible.
  See ADR-006.
- **Provenance metadata (Step 26):** `artifact_provenance` table (migration 007) tracks
  trust level per file: `human` (ADRs, specs), `agent` (default), `external` (web content,
  third-party data). Gateway upserts on every `write_artifact`; `read_artifact` and context
  packager look it up. External content is wrapped in `<external-content>` delimiters in
  agent prompts (both Python loop and Claude Code); provenance rule injected into every
  agent's instruction set.
- **Observability pass (Step 27):** Both FastAPI services expose `/metrics` via
  `prometheus-fastapi-instrumentator` (HTTP request counters + latency histograms) plus
  four application-level counters/histograms: task lifecycle, cumulative LLM cost, validator
  outcomes, human review queue latency. OTel FastAPI auto-instrumentation exports to Jaeger
  all-in-one when `OTLP_ENDPOINT` is set (no-op otherwise). Grafana pre-provisioned with
  Prometheus datasource and seven-panel Orchestra dashboard. See ADR-007.

**Next in queue:**
- **Per-write audit for claude-code-agent:** gateway intercept or post-commit diff audit.
- **Policy file for risk tiers:** configurable Tier 1/2 gates per project.
- **Validator provenance check:** refuse to validate a task whose outputs carry
  `provenance=external`.

### Phase 4 — Scale and polish (planned)

DAG visualisation UI, event replay CLI, cost-aware model routing, dynamic agent
spawning with inherited-and-narrowed capabilities, load testing, v1.0 doc.

---

## Key design decisions (ADRs)

| ADR | Decision |
|---|---|
| ADR-001 | Git as the artifact plane (not custom object storage) |
| ADR-002 | Postgres as the control plane (tasks, events, audit) |
| ADR-003 | Gateway-mediated side effects (agents have no direct access) |
| ADR-004 | Event sourcing for orchestrator state (append-only events table) |
| ADR-005 | Gateway as Phase 1 sandbox (subprocess on host; Docker isolation deferred) |
| ADR-006 | Capability tokens: HS256 JWT for gateway authorization, write-scope enforcement |
| ADR-007 | Observability: pull-based Prometheus, Jaeger all-in-one, OTel auto-instrumentation |

---

## Non-negotiable invariants

1. **Gateway-only side effects.** Agents never get raw Git credentials, direct DB access,
   or unmediated shell. All reads/writes/executions/event emissions go through the gateway.
2. **Two planes.** Git is the artifact plane. Postgres is the control plane. Never cross.
3. **Append-only events.** The `events` table is never updated or deleted.
4. **Explicit state machine.** Every status change goes through defined transitions;
   every transition writes an event and an audit row in one DB transaction.
5. **Nothing merges to `main` without the merge flow** (validator → human approval via
   `orchctl merge`). Tier 0 auto-merge is the only exception — and it still runs the validator.
6. **Provenance discipline.** External-provenance content is wrapped in delimiters when
   placed in prompts and never goes into system prompts.
