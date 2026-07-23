# Orchestra — Architecture Overview

## What it is

Orchestra is a human-governed multi-agent orchestration platform. You submit a goal in
plain English; a root agent decomposes it into tasks; specialist AI agents execute each
task inside a sandboxed repo; every merge back to your project requires human approval.

The three-way ownership model:

| Owner | Responsibility |
|-------|---------------|
| **Human** | Intent, approval, and merge decisions |
| **Agents** | Execution — reading, writing, running code |
| **Orchestrator** | Governance — task DAG, event log, state machine, audit |

---

## The two planes

Orchestra enforces a hard separation between its two planes:

**Artifact plane — your project repo (`SANDBOX_REPO_PATH`)**
This is the Git repository that agents operate on. Agents create branches, write files,
and commit here. Nothing merges to `main` without going through the human approval flow.

**Control plane — Postgres**
All task state, events, runs, audit rows, and agent memories live in Postgres. Never in Git.
The event log is append-only; orchestrator state can always be reconstructed by replaying it.

---

## Runtime components

```
┌───────────────────────────────────────────────────────────────┐
│  You (human)                                                  │
│    orchctl request / /orcui                                   │
└──────────────┬────────────────────────────────────────────────┘
               │ HTTP
┌──────────────▼────────────────────────────────────────────────┐
│  Root Agent  (agents/root/main.py)                            │
│    Accepts change requests, decomposes into task DAGs,        │
│    calls POST /tasks for each, emits REPLAN events            │
└──────────────┬────────────────────────────────────────────────┘
               │ POST /tasks, GET /tasks, PATCH /tasks/{id}/status
┌──────────────▼────────────────────────────────────────────────┐
│  Orchestrator  :8080  (orchestrator/orchestrator/api.py)      │
│    Task CRUD, state machine, event log, context packager,     │
│    validator runner, agent memory API                         │
└──────────────┬────────────────────────────────────────────────┘
               │ Redis Streams (TASK_ASSIGNED events)
┌──────────────▼────────────────────────────────────────────────┐
│  Dispatcher  (orchestrator/orchestrator/dispatcher.py)        │
│    Subscribes to the event bus, launches agent subprocesses,  │
│    manages DAG fan-out, retries, heartbeat watchdog,          │
│    writes episode + identity memory after task completion     │
└──────────────┬────────────────────────────────────────────────┘
               │ subprocess (context package JSON path)
┌──────────────▼────────────────────────────────────────────────┐
│  Agent Workers  (agents/backend/, agents/claude_code/, …)     │
│    Read context package, call LLM, write via gateway tools    │
└──────────────┬────────────────────────────────────────────────┘
               │ HTTP (capability-token-authenticated)
┌──────────────▼────────────────────────────────────────────────┐
│  Gateway  :8081  (gateway/gateway/app.py)                     │
│    Checks permissions, audits every side effect atomically,   │
│    proxies all reads/writes/commands/git ops/memory ops       │
└──────────────┬────────────────────────────────────────────────┘
               │ filesystem + git
┌──────────────▼────────────────────────────────────────────────┐
│  Your project repo  (SANDBOX_REPO_PATH)                       │
│    Where agents read and write; branches merged via gateway   │
└───────────────────────────────────────────────────────────────┘
```

### Ports

| Service | Default port | Purpose |
|---------|-------------|---------|
| Orchestrator | 8080 | Task API, state machine, event log |
| Gateway | 8081 | Audited tool proxy for agents |
| Postgres | 5433 (host) | Control plane database |
| Redis | 6380 (host) | Event bus (Redis Streams) |
| Jaeger UI | 16686 | Distributed traces (optional) |
| Prometheus | 9090 | Metrics (optional) |
| Grafana | 3000 | Dashboards (optional) |

---

## Non-negotiable invariants

These hold across every version of the platform:

1. **Gateway-only side effects.** Agents never get raw Git credentials or direct filesystem
   access. Every read, write, command, event, and git operation flows through the gateway,
   which checks permissions and writes an audit record atomically with the action.

2. **Two planes.** Artifacts (code, docs) live in Git. Control state (tasks, events, runs)
   lives in Postgres. Never cross the boundary.

3. **Append-only events.** The `events` table is never updated or deleted. Orchestrator
   state is always reconstructable by replaying the event log from scratch.

4. **Explicit state machine.** Task status only changes through defined transitions.
   Every transition writes an event and an audit row in a single DB transaction.
   See [Task Lifecycle](./task-lifecycle.md) for the full transition graph.

5. **Human merge gate.** Nothing merges to `main` of the managed repo without the
   human-approval flow: validate → human approves → merge via gateway.

6. **Provenance discipline.** External content (user-supplied spec files, fetched web
   pages) is wrapped in `<external-content>` delimiters in agent prompts and never placed
   in system prompts directly.

---

## Event bus

The orchestrator publishes every task status event to a Redis Stream (`orchestra:tasks`).
The dispatcher subscribes and reacts:

- `TASK_ASSIGNED` → launch the right agent subprocess
- `TASK_COMPLETED` → write episode memory, advance DAG successors, refresh identity
- `TASK_DISCOVERED` → create child task, block parent
- `TASK_FAILED` / `TASK_ESCALATED` → log and surface in `orchctl why`
- `TASK_SUSPENDED` → heartbeat watchdog detected missing ping
- `TASK_HUMAN_INPUT_REQUIRED` → wait for `orchctl respond`

---

## Security model

Each agent run receives a short-lived HS256 JWT (capability token) minted at run creation
and signed with `CAPABILITY_SECRET`. The gateway verifies this token on every request and
enforces the token's write-scope — agents can only write to paths declared in the task's
`outputs` list.

See [ADR-006](../adr/ADR-006-capability-tokens.md) for the full token design.

---

## Further reading

- [Quickstart](./quickstart.md) — get running in 10 minutes
- [Task Lifecycle](./task-lifecycle.md) — state machine, all statuses explained
- [Agents](./agents.md) — agent types, how they run, how to choose
- [Validators](./validators.md) — pluggable validator registry
- [Memory](./memory.md) — agent memory system
- [CLI Reference](./cli-reference.md) — every `orchctl` command
- [Configuration](./configuration.md) — environment variables and config files
- [API Reference](./api-reference.md) — orchestrator and gateway HTTP APIs
- [Design doc](../design/orchestrator-mvp-v0.2.md) — full architecture specification
