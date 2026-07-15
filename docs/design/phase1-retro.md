# Phase 1 Retrospective

**Scope completed:** Walking skeleton — one human, one backend agent, real Git, real audit.
**Date:** 2026-07-14

---

## What shipped

All thirteen Phase 1 steps are done:

1. Monorepo setup, Python 3.12, uv, ruff, pytest
2. JSON Schemas for Task, Event, AgentIdentity, RunRecord (Pydantic v2 from `schemas/`)
3. Postgres via Docker Compose; `tasks`, `events`, `runs`, `audit` tables; Alembic migrations
4. ADR-001 through ADR-005 (Git-as-artifact-plane, Postgres-as-control-plane, gateway-mediated side effects, event sourcing, Phase 1 subprocess sandbox)
5. Task CRUD API + `orchctl create-task / list / approve`
6. Task state machine (created → assigned → running → completed → validated → merged → closed, plus failed/escalated arms); every transition atomic with event + audit
7. Context packager: task spec + input artifacts + ADRs → JSON package written to disk with Run row
8. Tool gateway on port 8081: read/write artifact, run command, emit event, git branch/commit/merge; every call permission-checked and audited
9. Backend agent loop: LLM tool-use loop, task_complete triggers commit + transition
10. Structured commit messages: `[TASK-ID] message` via `commit_prefix` in every context package
11. Validator: `ruff check` + `pytest` on the agent branch; result written as event; transition to validated or failed
12. Merge flow: `orchctl merge` calls gateway `/git/merge` (audited), then orchestrator transitions validated → merged → closed
13. End-to-end demo: `make demo` runs the full flow on the sample FastAPI project

**Test count:** 142 passing tests, no mocks against the DB or gateway in the agent tests (those use real Postgres and real subprocess).

---

## What hurt

### 1. Postgres disk exhaustion (three times)

The Docker pgdata volume filled up during test runs. The symptom is always `No space left on device` from psycopg, and the fix is always the same: `make down && docker volume rm orchestra_pgdata && make up && make migrate`. The root cause is that every per-test rolled-back transaction still generates WAL, and the volume is small.

**Mitigation for Phase 2:** Set `max_wal_size` and `min_wal_size` in the Postgres config, and add a `make clean-db` target so the reset is one command.

### 2. Step numbering drift

Step 10 in the design doc ("Structured commit messages") was absorbed into step 9 (backend agent loop), because the commit prefix is just a field in the context package. This created confusion when referring to "step 10" — it could mean the design doc's step 10 (already done) or the next pending step (the validator). The design doc numbering should be treated as a reference, not a checklist.

### 3. Gateway permission model for merge

The existing `check_active_run` permission check requires `task.status == "running"`, which is exactly wrong for the merge endpoint (the task must be `validated`). This required adding a separate `check_validated_task` function. The two-function pattern is fine for Phase 1 but signals that the permission model will need a clean abstraction in Phase 3 when capability tokens arrive.

### 4. CLI did not expose inputs/outputs on create-task

The `orchctl create-task` command did not have `--input`/`--output` flags, even though the `TaskCreate` API body accepted them. This was discovered when writing the demo script. The fix was a one-line addition but the gap existed for the whole of Phase 1 because the integration tests used the API directly.

### 5. The agent's actor identity is set by task.owner

The gateway permission check uses `agent_id` from the active Run, which is set at `run-task` time. The agent loop uses `context_package["task"]["owner"]` as its `agent_id` for gateway calls. If these two disagree — e.g., task created with `--owner human` but `run-task` uses `--agent-id backend-agent` — the gateway returns 403 on every call. The demo script must set `--owner backend-agent` to avoid this. Phase 3 capability tokens will make this explicit rather than a convention.

---

## What worked well

- **Two-plane discipline** held throughout. Nothing tempted us to put artifacts in Postgres or task state in Git.
- **Append-only events** meant that every state transition is auditable and the event log is already a valid replay source.
- **Gateway as the sole side-effect boundary** caught one accidental direct-subprocess call in the validator (which was intentionally direct, since the validator is orchestrator infrastructure, not an agent).
- **Per-test rolled-back DB sessions** made the test suite fast and isolated with zero test-data cleanup code.
- **Real git repos in tests** (no mocking of subprocess) meant the git/branch/commit/merge tests actually verified git behavior.

---

## Phase 2 priorities (in order)

1. Redis Streams event bus — replace direct orchestrator→agent calls with event-driven dispatch
2. DAG scheduling — dependency gating so tasks with `depends_on` only dispatch when unblocked
3. Second agent (docs or QA) — proves the multi-agent pattern before adding frontend
4. Fix Postgres disk pressure — `max_wal_size` config + `make clean-db` target
