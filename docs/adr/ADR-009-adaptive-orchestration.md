# ADR-009: Adaptive Orchestration (v0.3)

**Status:** Accepted
**Date:** 2026-07-19

## Context

Phase 3 introduced a persistent root agent that decomposes change requests into a static
task DAG at plan time. Static plans fail when execution reveals unexpected work: a
migration that an auth task depends on, a schema the frontend needs but no one planned
for. Agents either stall (trying to write outside their scope) or silently skip the
dependency (producing broken output).

v0.3 adds adaptive orchestration: agents can discover work at runtime, the platform
creates child tasks on their behalf, and parent tasks suspend and resume around that
child work. Every step goes through the gateway and state machine so the CLAUDE.md
invariants remain intact.

## Decision

### 1. Event-driven discovery over polling

Agents emit `TASK_DISCOVERED` through the gateway's `/emit_event` endpoint. The gateway
writes the event to Postgres and publishes it to the `orchestra:events` Redis stream. The
Dispatcher consumes the event and delegates to the Scheduler.

Polling (agent periodically asks "can I create a child?") was rejected because it would
require a new interrogation API, race conditions between poll and action, and no audit
trail at the decision moment. Events give an atomic, append-only record that is
reconstructable from the `events` table alone.

### 2. `blocked` status over an external dependency table

The `tasks` table gains four columns: `parent_task_id`, `spawn_depth`, `blocked_by`
(JSONB list of child IDs), and `checkpoint` (JSONB agent state). A new `blocked` status
sits between `running` and `assigned`:

```
running → blocked   (TASK_BLOCKED event)
blocked → assigned  (TASK_RESUMED event, triggered by Scheduler.on_child_terminal)
```

`blocked` is not terminal and does not satisfy `task_is_ready()` for any dependent task.

An alternative of a separate `task_dependencies_dynamic` join table was considered. It
would be cleaner relationally, but it would require a new query on every DAG readiness
check, a new migration, and extra surface area in every tool that traverses the task
graph. The JSONB column approach keeps the task row self-describing and the state machine
complete with no external joins.

### 3. Capability narrowing at `create_run` time

Child tasks receive a capability token whose `write_scope` is
`child.outputs ∩ parent.outputs` (prefix-match intersection). This is computed in
`mint_child_capability_token()` in `token.py` and enforced at gateway `/git/commit`.

The Scheduler also validates this constraint before creating the child: if any requested
output falls outside the parent's scope the discovery is rejected with
`outputs_outside_parent_scope`. The gateway commit check is the belt-and-suspenders layer
for the claude-code agent (which writes to disk directly; the gateway is the only choke
point for its git operations).

The alternative of inheriting the full parent scope was rejected: it would allow a
malicious or confused agent to discover a child claiming write access to paths the parent
never touched, defeating the purpose of scoped tokens.

### 4. Scheduler in-process inside Dispatcher

The `Scheduler` class lives inside `orchestrator/orchestrator/scheduler.py` and is
instantiated once in the `Dispatcher.__init__`. It performs pure DB mutations (no commit,
no Redis publish). The Dispatcher owns all commits and publishes — this keeps the
transaction boundary clear and avoids the Scheduler needing its own Redis connection.

A separate Scheduler service was considered but rejected: it would require a new port,
network call overhead on every discovery, and additional deployment surface. The
Dispatcher is already the single process consuming the event stream; co-locating the
Scheduler avoids a second hop for what is purely a DB mutation.

## Consequences

- **New state machine transitions** — `running → blocked` and `blocked → assigned` must
  be represented in the TRANSITIONS dict and tested. `blocked` tasks are invisible to
  `get_ready_successors()` so downstream tasks cannot start until the parent is resumed.
- **Context packages for resumed tasks** carry `is_resumption=True`, the checkpoint dict,
  and `child_outputs` so agents can continue from where they left off without repeating
  work.
- **Planner re-entry** (Stage 5) — when a discovered child has pending dependencies, the
  Dispatcher publishes `PLAN_REPLAN_REQUESTED` to the `root:requests` stream so the root
  agent can decide whether to add or re-order pending tasks.
- **`MAX_SPAWN_DEPTH = 5`** (env-configurable) and **`MAX_BLOCKED_BY = 10`** caps prevent
  runaway recursion and fan-out.
- **Prometheus metrics** — `tasks_discovered_total`, `tasks_blocked_total`,
  `tasks_resumed_total`, `task_discovery_rejected_total{reason}`, and
  `spawn_depth_histogram` are emitted by the Scheduler so the Grafana dashboard can
  surface adaptive activity in real time.

## Alternatives considered

- **Agent spawns agents directly** — violates the gateway-only side-effects invariant and
  removes audit coverage. Rejected.
- **Polling-based child creation** — see decision 1 above. Rejected.
- **External dependency table** — see decision 2 above. Deferred; can be added later if
  the JSONB approach proves insufficient at scale.
- **Separate Scheduler service** — see decision 4 above. Rejected for Phase 3.
