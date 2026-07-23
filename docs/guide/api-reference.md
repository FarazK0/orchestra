# API Reference

Orchestra exposes two HTTP services. The **Orchestrator** (port 8080) owns the control
plane: tasks, events, runs, memories. The **Gateway** (port 8081) owns all audited
side effects: file reads/writes, commands, git operations, memory upserts.

Agents must talk to the gateway for side effects. Humans and tools use both.

---

## Orchestrator API — port 8080

Base URL: `http://localhost:8080`

### `GET /healthz`

Health check.

```json
{"status": "ok"}
```

---

### `GET /validators`

Return the full validator registry from `permissions/validators.yaml`.

```json
{
  "validators": [
    {
      "name": "ruff",
      "description": "Python linter (ruff check .)",
      "auto_detect": true,
      "always_run": false,
      "match_extensions": [".py"]
    }
  ]
}
```

---

### `POST /tasks`

Create a new task.

**Request body:**
```json
{
  "title": "Implement login endpoint",
  "owner": "backend-agent",
  "inputs": ["spec.md"],
  "outputs": ["app/auth.py", "tests/test_auth.py"],
  "acceptance": ["POST /login returns 200 with valid credentials"],
  "validators": ["ruff", "pytest"],
  "depends_on": []
}
```

`validators` is optional. If omitted or `null`, the orchestrator auto-detects
validators from `outputs`. Pass `[]` explicitly to assign no validators.

**Response:** full `TaskSchema` object including the auto-assigned `id` (`TASK-001` etc.),
`risk_tier` (resolved from `policy.yaml`), and `budget`.

---

### `GET /tasks`

List tasks.

Query params:
- `status` — filter by status string

**Response:**
```json
[{"id": "TASK-001", "title": "...", "status": "created", ...}]
```

---

### `GET /tasks/{task_id}`

Get a single task by ID.

---

### `PATCH /tasks/{task_id}/status`

Transition a task to a new status.

**Request body:**
```json
{
  "new_status": "assigned",
  "actor": "human",
  "payload": {},
  "details": {}
}
```

Returns the updated task. Raises 400 if the transition is invalid. Raises 409 for
Tier 2 tasks transitioning to `merged` without `details.tier2_override = true`.

---

### `POST /tasks/{task_id}/respond`

Inject a human answer into a task in `awaiting_human` status. Re-queues the task
as `assigned`.

**Request body:**
```json
{"answer": "Use PostgreSQL, not SQLite"}
```

---

### `GET /tasks/{task_id}/events`

List all events for a task, oldest first.

---

### `POST /tasks/{task_id}/runs`

Start a run for a task in `assigned` status. Assembles the context package, mints a
capability token, writes the package to `RUN_STORE_DIR`, and transitions to `running`.

**Request body:**
```json
{
  "agent_id": "backend-agent",
  "repo_path": "/path/to/your-project",
  "gateway_url": "http://localhost:8081",
  "orchestrator_url": "http://localhost:8080"
}
```

**Response:**
```json
{
  "run_id": "...",
  "context_package_path": "/tmp/orchestra/runs/<run_id>.json"
}
```

---

### `POST /tasks/{task_id}/validate`

Run all assigned validators on the task's agent branch.

**Request body:**
```json
{
  "repo_path": "/path/to/your-project",
  "actor": "validator"
}
```

**Response:**
```json
{
  "passed": true,
  "branch": "agent/backend/TASK-001",
  "summary": "4/4 checks passed",
  "checks": [
    {"name": "file-exists", "passed": true, "output": "All 2 files present", "duration_s": 0.1},
    {"name": "ruff",        "passed": true, "output": "All checks passed",   "duration_s": 1.2},
    {"name": "pytest",      "passed": true, "output": "12 passed in 3.1s",   "duration_s": 3.1},
    {"name": "llm-acceptance", "passed": true, "output": "2/2 criteria met", "duration_s": 5.0}
  ]
}
```

---

### `GET /tasks/{task_id}/validation`

Return the most recent validation result for a task. Reads from `audit_rows.details`
linked to the most recent `TASK_VALIDATED` or `TASK_FAILED` event. Works after the
task is closed.

**Response:**
```json
{
  "validation": {
    "passed": true,
    "summary": "4/4 checks passed",
    "checks": [...]
  }
}
```

---

### `GET /tasks/{task_id}/runs`

List all runs for a task, with start time, finish time, result, tokens used, and cost.

---

### `GET /tasks/{task_id}/audit`

List all audit rows for a task (gateway operations), most recent first.

---

### `POST /replan`

Trigger a root agent replan (used internally by agents that call `discover_task`).

---

### Memory endpoints (orchestrator)

#### `POST /agent-memories`

Upsert a memory row (control-plane write, not audited by gateway).

#### `GET /agent-memories`

List memory rows.

Query params: `agent_id`, `project_id`, `memory_type`, `key`

#### `DELETE /agent-memories/{memory_id}`

Delete a memory row.

---

## Gateway API — port 8081

Base URL: `http://localhost:8081`

All requests from agents must include:
```
Authorization: Bearer <capability_token>
```

The capability token is a HS256 JWT minted at run creation. It encodes:
- `task_id` — the task this run belongs to
- `agent_id` — the agent making the request
- `run_id` — the specific run
- `write_scope` — list of paths the agent may write to
- `exp` — expiry (4 hours from minting)

Platform-internal callers (dispatcher, root agent) use `X-Platform-Actor: <name>`
instead of a capability token.

### `GET /healthz`

Health check.

### `POST /heartbeat`

Agent heartbeat. Must be called every ≤60 s by running agents.

**Request body:**
```json
{"run_id": "...", "task_id": "TASK-001", "agent_id": "backend-agent"}
```

Sets a Redis key with a 180 s TTL. The dispatcher watchdog suspends tasks whose key
expires.

---

### `POST /read_artifact`

Read a file from the target repo.

**Request body:**
```json
{"path": "app/models.py"}
```

**Response:**
```json
{"content": "...", "path": "app/models.py"}
```

Audited. Any path is readable; reads are not scope-restricted.

---

### `POST /write_artifact`

Write a file to the target repo.

**Request body:**
```json
{"path": "app/auth.py", "content": "..."}
```

**Write-scope enforcement:** the path must be in the capability token's `write_scope`
(which is derived from `task.outputs`). Returns 403 if the path is out of scope.

Audited.

---

### `POST /run_command`

Run a shell command in the target repo.

**Request body:**
```json
{"command": "pytest --tb=short -q", "timeout": 120}
```

**Response:**
```json
{"returncode": 0, "stdout": "...", "stderr": ""}
```

Audited. Commands run with the target repo as the working directory.

---

### `POST /emit_event`

Write a structured event to the orchestrator's event log.

**Request body:**
```json
{
  "event_type": "TASK_COMPLETED",
  "task_id": "TASK-001",
  "payload": {"paths_changed": ["app/auth.py", "tests/test_auth.py"]}
}
```

Standard event types emitted by agents:
- `TASK_COMPLETED` — agent finished; triggers `completed` transition
- `TASK_FAILED` — agent failed; triggers `failed` transition
- `HUMAN_ATTENTION_NEEDED` — agent needs human input; triggers `awaiting_human`
- `TASK_DISCOVERED` — agent requests a child task; dispatcher creates it
- `QA_REPORT_FILED` — QA agent filed a passing report
- `QA_ISSUE_FOUND` — QA agent found a blocking issue

Audited.

---

### `POST /git/branch`

Create or checkout a branch in the target repo.

**Request body:**
```json
{"branch": "agent/backend/TASK-001", "create": true}
```

Audited.

---

### `POST /git/commit`

Stage paths and commit to the current branch.

**Request body:**
```json
{
  "paths": ["app/auth.py", "tests/test_auth.py"],
  "message": "feat: implement login endpoint"
}
```

Audited.

---

### `POST /git/merge`

Merge the agent's branch into `main`. Only callable after the task is in `validated`
status. The gateway verifies status before executing.

**Request body:**
```json
{
  "task_id": "TASK-001",
  "source_branch": "agent/backend/TASK-001",
  "target_branch": "main",
  "tier2_override": false
}
```

Audited. Transitions task: `validated → merged → closed`.

---

### `POST /memory/upsert`

Upsert an agent memory row. Content is capped at 2000 characters.

**Request body:**
```json
{
  "task_id": "TASK-001",
  "agent_id": "backend-agent",
  "project_id": "default",
  "memory_type": "skill",
  "key": "skill/http-convention",
  "content": "Always use httpx.AsyncClient..."
}
```

Write restrictions:
- Agents (capability token auth) may only write `memory_type = "skill"`
- Platform actors (`X-Platform-Actor` header) may write any type
- Same-topic skill rows are merged (content replaced + timestamp updated)

Audited.

---

### `POST /memory/search`

Keyword search over the agent's own memories and the shared pool (`agent_id = "shared"`).

**Request body:**
```json
{"query": "database conventions", "limit": 5}
```

**Response:**
```json
{
  "results": [
    {"key": "skill/postgres-conventions", "content": "...", "memory_type": "skill"}
  ]
}
```

Derives `agent_id` from `task.owner` based on the capability token's `task_id`.
Audited.

---

### `POST /human_input/request`

Called by agents to pause and request human input. Transitions the task to
`awaiting_human` and stores the question for `orchctl questions` / `orchctl respond`.

**Request body:**
```json
{
  "task_id": "TASK-001",
  "run_id": "...",
  "question": "Should I use PostgreSQL or SQLite for this feature?"
}
```
