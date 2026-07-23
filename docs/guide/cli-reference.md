# CLI Reference — `orchctl`

All commands are available as `orchctl <command>` (after `make install`) or
`./orchctl <command>` from the repo root (after `uv sync`, no global install needed).

Global option: `--help` on any command prints usage.

---

## Task flow

### `orchctl request`

Submit a plain-English change request to the root agent. The root agent decomposes
it into tasks, creates them in the orchestrator, and dispatches agents automatically.

```bash
orchctl request "add a login page with email/password auth" [--spec PATH]
```

Options:
- `--spec PATH` — path to a spec file (relative to `SANDBOX_REPO_PATH`) that the
  root agent reads for additional context

The root agent runs synchronously until it has submitted all tasks, then exits. Tasks
are visible in `orchctl list` immediately.

---

### `orchctl create-task`

Create a single task manually. Prompts interactively to accept or edit the
auto-detected validator list.

```bash
orchctl create-task TITLE \
    [--owner AGENT_ID] \
    [--accept "criterion"] \
    [--input PATH] \
    [--output PATH] \
    [--depends-on TASK-ID]
```

Options:
- `--owner` — agent to assign. One of: `backend-agent`, `frontend-agent`, `qa-agent`,
  `claude-code-agent`. Default: `backend-agent`
- `--accept` — acceptance criterion (repeatable)
- `--input` — input file path in the target repo (repeatable)
- `--output` — output file path in the target repo (repeatable); used for auto-detection
  of validators and write-scope enforcement
- `--depends-on` — task ID this task waits for (repeatable)

Example:
```bash
orchctl create-task "Implement user auth" \
    --owner backend-agent \
    --output app/auth.py \
    --output tests/test_auth.py \
    --accept "POST /login returns 200 with valid credentials" \
    --accept "POST /login returns 401 with invalid credentials"
```

---

### `orchctl list`

List tasks, newest last.

```bash
orchctl list [--status STATUS]
```

Options:
- `--status STATUS` — filter by status: `created`, `assigned`, `running`, `completed`,
  `validated`, `merged`, `closed`, `failed`, `escalated`, `suspended`, `awaiting_human`,
  `blocked`, `cancelled`

---

### `orchctl show`

Show full detail for a task, including inputs, outputs, validators, acceptance criteria,
and the most recent validation result (per-check table).

```bash
orchctl show TASK-001
```

Works on tasks in any status, including closed ones.

---

### `orchctl approve`

Advance a task through the current human gate.

```bash
orchctl approve TASK-001
```

- `created → assigned` — authorises the dispatcher to launch the agent
- `validated → merged` — triggers the merge flow (gateway merges branch into `main`)

The same command handles both gates; the orchestrator determines which transition to
apply based on the task's current status.

---

### `orchctl run-task`

Manually assemble a context package and launch an agent run. Usually not needed —
the dispatcher does this automatically after `orchctl approve`. Use for debugging.

```bash
orchctl run-task TASK-001 --repo PATH [--agent-id AGENT_ID]
```

Options:
- `--repo PATH` — absolute path to the target repo (required)
- `--agent-id AGENT_ID` — must match `task.owner` for gateway auth

---

### `orchctl validate`

Run all assigned validators on a completed task's agent branch.

```bash
orchctl validate TASK-001 --repo PATH [--actor NAME]
```

Options:
- `--repo PATH` — absolute path to the target repo (required)
- `--actor NAME` — actor name recorded in the audit log (default: `validator`)

Displays a per-check table. Full output for failing checks is printed below the table.
On pass: transitions to `validated`. On any fail: transitions to `failed`.

---

### `orchctl merge`

Merge a validated task's agent branch into `main` of the target repo and close the task.

```bash
orchctl merge TASK-001 --repo PATH [--tier-2-override]
```

Options:
- `--repo PATH` — absolute path to the target repo (required)
- `--tier-2-override` — required for Tier 2 tasks (permissions, migrations, schemas)

Internally calls `POST /git/merge` on the gateway, which verifies task status and
performs the merge atomically with the status transition.

---

### `orchctl review`

Interactive approval loop. Polls for completed tasks, runs validation automatically,
shows per-check results, and prompts for merge.

```bash
orchctl review --repo PATH [--yes]
```

Options:
- `--repo PATH` — absolute path to the target repo (required)
- `--yes` — auto-approve all validated tasks without prompting (use with care)

This is the primary end-to-end approval workflow. Run it once and it handles all
pending tasks in order.

---

### `orchctl cancel`

Cancel a task from any non-terminal state.

```bash
orchctl cancel TASK-001 [--reason TEXT]
```

Options:
- `--reason TEXT` — reason recorded in the audit log

---

### `orchctl recover`

Mark an escalated task as `completed` so it can be validated and merged. Use when
an agent actually finished its work but the platform classified it as failed.

```bash
orchctl recover TASK-001
```

---

### `orchctl resume`

Re-queue a suspended task. The dispatcher will re-launch the agent on the same branch,
continuing from the last git commit.

```bash
orchctl resume TASK-001
```

---

### `orchctl questions`

List all tasks currently in `awaiting_human` status — agents waiting for human input.

```bash
orchctl questions
```

Shows the task ID, title, and the question the agent asked.

---

### `orchctl respond`

Send an answer to an agent waiting in `awaiting_human` state. Injects the answer into
the next run's context package and re-queues the task as `assigned`.

```bash
orchctl respond TASK-001 "your answer here"
```

---

### `orchctl tail`

Stream the live agent log for a running task.

```bash
orchctl tail TASK-001
```

Follows the log file at `/tmp/orchestra/logs/<run-id>.log`. Exits when the task leaves
`running` status.

---

### `orchctl audit`

Show the gateway audit trail for a task — all reads, writes, commands, and git
operations the agent performed, most recent first.

```bash
orchctl audit TASK-001
```

---

### `orchctl why`

Show a diagnostic panel explaining why a task failed or escalated. Includes the last
agent log lines, the failure event payload, and suggested remediation.

```bash
orchctl why TASK-001
```

---

## Validators

### `orchctl validator list`

List all validators in `permissions/validators.yaml` with name, auto-detect flag,
and description.

```bash
orchctl validator list
```

---

## Agent memory

### `orchctl memory list`

List agent memory entries.

```bash
orchctl memory list [--agent AGENT_ID] [--type TYPE] [--project PROJECT]
```

Options:
- `--agent AGENT_ID` — filter by agent (e.g. `backend-agent`)
- `--type TYPE` — filter by type: `identity`, `episode`, `skill`
- `--project PROJECT` — filter by project ID (default: `default`)

---

### `orchctl memory show`

Show the full content of a memory entry.

```bash
orchctl memory show MEMORY_ID [--agent AGENT_ID]
```

`MEMORY_ID` can be a full UUID or an 8-character prefix.

---

### `orchctl memory delete`

Delete a memory entry. Writes an audit record before deletion.

```bash
orchctl memory delete MEMORY_ID [--agent AGENT_ID] [--reason TEXT] [--yes]
```

Options:
- `--reason TEXT` — reason recorded in the audit log
- `--yes` — skip confirmation prompt

---

## Agent identity

### `orchctl identities`

List agent identity profiles with domain expertise, task history, and skill breakdown.

```bash
orchctl identities [--agent AGENT_ID]
```

---

### `orchctl teach`

Inject a skill or fact directly into an agent's memory. The memory is stored with
key `skill/human/<topic>` and is marked as human-taught.

```bash
orchctl teach AGENT_ID "fact or instruction" [--topic TOPIC]
```

Options:
- `--topic TOPIC` — slug used as the memory key suffix (default: auto-generated from text)

Example:
```bash
orchctl teach backend-agent \
    "Always use httpx.AsyncClient for async HTTP, never requests." \
    --topic http-client-convention
```

---

### `orchctl forget`

Remove a human-taught skill from an agent's memory by topic slug or 8-char memory ID.
Only removes memories with key prefix `skill/human/`.

```bash
orchctl forget AGENT_ID TOPIC_OR_ID [--yes]
```

---

### `orchctl ask`

One-shot competency probe — ask a question grounded in the agent's identity and skill
memories. Useful for verifying what an agent knows before assigning it a task.

```bash
orchctl ask AGENT_ID "question" [--model MODEL]
```

Uses the backend configured by `orchctl config set llm-backend`.

---

### `orchctl session`

Multi-turn interactive identity session with an agent. The agent's memories are loaded
as a system prompt and you can have a conversation about the project.

```bash
orchctl session AGENT_ID [--model MODEL]
```

With the `claude` backend this launches an interactive `claude` subprocess.
With the `python` backend this runs a REPL using `LLMClient`.

---

## Configuration

### `orchctl config show`

Show current orchctl session configuration, including the active LLM backend and
whether each backend's prerequisites are available.

```bash
orchctl config show
```

---

### `orchctl config set`

Set a configuration value. Saved to `~/.config/orchestra/config`.

```bash
orchctl config set KEY VALUE
```

Supported keys:
- `llm-backend` — `claude` (default) or `python`

Example:
```bash
orchctl config set llm-backend python
```

---

## Other

### `orchctl quickstart`

Print a getting-started cheat-sheet. Works offline — no services needed.

```bash
orchctl quickstart
```

### `orchctl --help`

Show the full command list.

```bash
orchctl --help
orchctl COMMAND --help
```
