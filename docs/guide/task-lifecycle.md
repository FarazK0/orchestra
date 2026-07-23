# Task Lifecycle

Every task in Orchestra moves through a defined state machine. Status changes are
atomic — each transition writes an event row and an audit row in a single database
transaction, and nothing outside the state machine can update `tasks.status`.

---

## Status reference

| Status | Meaning |
|--------|---------|
| `created` | Task exists; awaiting human approval to assign it to an agent |
| `assigned` | Human approved; dispatcher will pick it up and launch an agent |
| `running` | Agent subprocess is active; heartbeat pings every 60 s |
| `blocked` | Parent task suspended while a child task runs (v0.3 adaptive lifecycle) |
| `suspended` | Agent was interrupted (process killed, API down, credits exhausted); resumable |
| `awaiting_human` | Agent emitted `HUMAN_ATTENTION_NEEDED`; waiting for `orchctl respond` |
| `completed` | Agent called `task_complete`; awaiting validation |
| `validated` | All assigned validators passed; awaiting human approval to merge |
| `merged` | Agent branch merged to `main` of the target repo |
| `closed` | Terminal state; all work done and on `main` |
| `failed` | Validator failed, or agent emitted a failure event |
| `escalated` | Retries exhausted; requires human intervention |
| `cancelled` | Human cancelled from any non-terminal state |

---

## Full transition graph

```
                    ┌──────────┐
                    │ created  │◄─────────── orchctl create-task
                    └────┬─────┘
                         │ orchctl approve
                         ▼
                    ┌──────────┐
              ┌────►│ assigned │◄──── orchctl resume (from suspended)
              │     └────┬─────┘      orchctl respond (from awaiting_human)
              │          │ dispatcher launches agent
              │          ▼
              │     ┌──────────┐
              │  ┌──│ running  │──┐
              │  │  └────┬─────┘  │
              │  │       │        │ heartbeat expires
              │  │       │        ▼
              │  │       │   ┌───────────┐
              │  │       │   │ suspended │──► orchctl resume ──► assigned
              │  │       │   └───────────┘
              │  │       │
              │  │       │ agent asks a question
              │  │       ▼
              │  │  ┌────────────────┐
              │  │  │ awaiting_human │──► orchctl respond ──► assigned
              │  │  └────────────────┘
              │  │
              │  │ child task spawned
              │  ▼
              │  ┌─────────┐
              │  │ blocked │──► child completes ──► assigned
              │  └─────────┘
              │
              │ task_complete called
              ▼
         ┌───────────┐
         │ completed │
         └─────┬─────┘
               │ orchctl validate
               ▼
    ┌─────┐  pass  ┌───────────┐
    │failed│◄──────│ validated │──► orchctl approve ──► merged ──► closed
    └──┬───┘       └───────────┘
       │
       │ orchctl cancel (from failed)
       ▼
  ┌───────────┐   retries exhausted
  │ escalated │◄──────────────────────── (from failed via dispatcher)
  └───────────┘
       │
       └──► orchctl recover ──► completed (manual bypass)
```

---

## Human gates

There are two points where a human must act before the task advances:

**Gate 1 — `created → assigned`**
```bash
orchctl approve TASK-001
```
Assigns the task to its declared owner agent. The dispatcher picks it up from the
Redis Stream and launches the agent subprocess.

**Gate 2 — `validated → merged`**
```bash
orchctl approve TASK-001
```
Triggers the merge: the gateway checks out the agent's branch, merges it into `main`
of the target repo, and advances the task to `closed`.

Both gates use the same `orchctl approve` command; the orchestrator resolves which
transition to make based on the current status.

---

## Validation

After an agent calls `task_complete` (status → `completed`), a human runs:

```bash
orchctl validate TASK-001 --repo /path/to/your-project
```

The orchestrator:
1. Checks out the agent's branch (`agent/<owner>/<task-id>`)
2. Runs every validator assigned to the task (see [Validators](./validators.md))
3. On pass: transitions to `validated`, stores full per-check results in the audit log
4. On fail: transitions to `failed`, stores per-check output for diagnosis

The `orchctl review` command automates gates 1 + 2 + validation in an interactive loop.

---

## Failure and recovery

**`failed` status** — the task can be:
- Retried automatically by the dispatcher (up to the configured retry budget)
- Cancelled: `orchctl cancel TASK-001`
- Investigated: `orchctl why TASK-001`

**`escalated` status** — retries exhausted. Options:
- `orchctl why TASK-001` — read the failure diagnostic
- `orchctl cancel TASK-001` — cancel and create a new task with a clearer spec
- `orchctl recover TASK-001` — if the agent actually finished its work but the
  platform misclassified it as failed, this moves it back to `completed` so it
  can be validated and merged normally

**`suspended` status** — the heartbeat watchdog detected that the agent process
stopped sending pings (every 60 s; key expires after 180 s). Options:
- `orchctl resume TASK-001` — re-queues the task as `assigned`; the dispatcher
  re-launches the agent on the same branch, continuing from the last git commit

**`awaiting_human` status** — the agent emitted `HUMAN_ATTENTION_NEEDED` and is
blocking, waiting for input:
- `orchctl questions` — list all tasks waiting for human answers
- `orchctl respond TASK-001 "your answer"` — inject the answer and re-queue

---

## Audit trail

Every transition, gateway operation, and validation result writes an audit row. To
inspect the full history of a task:

```bash
orchctl audit TASK-001        # gateway operations (reads, writes, commands, git ops)
orchctl show TASK-001         # full task detail + most recent validation result
```

The orchestrator also exposes `GET /tasks/{id}/events` for the raw event log.

---

## Risk tiers

Each task is assigned a risk tier (0, 1, or 2) based on its output paths, resolved
against `permissions/policy.yaml`:

| Tier | Meaning | Merge behaviour |
|------|---------|-----------------|
| 0 | Low risk (docs, tests) | Auto-mergeable (Tier 0 path) |
| 1 | Standard (most code) | Human approval required |
| 2 | High risk (permissions, migrations, schemas) | Human approval + explicit `--tier-2-override` flag |

```bash
orchctl merge TASK-001 --repo PATH --tier-2-override   # required for Tier 2
```

See [ADR-008](../adr/ADR-008-tier-policy.md) for the full tier policy design.
