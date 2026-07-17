# Orchestrator (control plane)

Owns task CRUD and state machine, event log (append-only), run records, context packaging, DAG scheduling, agent memory management, and the Redis Streams event bus integration.

## Modules

- `db.py` -- SQLAlchemy ORM models: `Task`, `Event`, `Run`, `AuditRow`, `StreamDelivery`, `AgentMemory`
- `state_machine.py` -- explicit task transitions; every transition writes an Event + AuditRow in one transaction; valid states: `created -> assigned -> running -> completed -> validated -> merged -> closed`; failure arm: `failed -> (retry -> running) | escalated`; cancel reachable from any non-terminal state
- `context_packager.py` -- assembles context package JSON per run: task spec, input artifacts, ADRs, agent memory (top-K per type, shared pool); touches `last_used_at` on every injected memory row
- `api.py` -- FastAPI app consumed by orchctl and the gateway; endpoints for task CRUD, state transitions, event log, run records, agent memory CRUD
- `dispatcher.py` -- event-driven task dispatch via Redis Streams consumer group; handles `TASK_ASSIGNED` (launch agent), `TASK_COMPLETED`/`TASK_MERGED` (advance DAG successors), `TASK_VALIDATED` (Tier 0 auto-merge), `TASK_FAILED` (retry or escalate); writes episode memory after each task completion
- `dag.py` -- topological readiness check; `get_ready_successors()` returns tasks whose `depends_on` are all in closed/merged state; `get_running_conflicts()` detects overlapping output paths
- `streams.py` -- Redis Streams publisher and consumer with consumer-group dedup and pending-entry reclaim
- `validator.py` -- runs `ruff check` and `pytest` on the agent branch; writes result as event

## Postgres tables

| Table | Purpose |
|---|---|
| `tasks` | Task CRUD, state, DAG edges |
| `events` | Append-only event log; never updated or deleted |
| `runs` | One row per agent run; links task to context package on disk |
| `audit_rows` | Every gateway operation and state transition; FK to events |
| `stream_deliveries` | Redis Streams dedup table; prevents double-processing |
| `agent_memories` | Persistent agent memory: identity, episode, skill, convention; unique on `(agent_id, project_id, key)` |
