# Agents

Orchestra supports two categories of agent worker. The choice affects how tasks are
executed and what credentials are required, but not the task model, DAG, or audit trail —
those are identical in both cases.

---

## Agent types

### `claude-code-agent` (recommended)

Launches the `claude` CLI as a subprocess. The CLI handles its own authentication
(`claude login`) and manages the full LLM conversation internally.

**Pros**
- No `ANTHROPIC_API_KEY` needed in `.env`
- Uses the same `claude` session you are already authenticated with
- Full Claude Code toolset available inside the agent (web search, MCP, etc.)
- Best raw capability for mixed tasks that span multiple layers

**Cons**
- Individual file writes are not separately audited through the gateway (the CLI does
  its own writes; branch creation and git commit still go through the gateway)
- Less predictable token/cost accounting (usage is in the Claude Code billing dashboard,
  not in Orchestra's `runs` table)

**How it runs**

```
Dispatcher
  └─► python -m agents.claude_code.main
        --context /tmp/orchestra/runs/<run_id>.json
        --run-id <uuid>
        [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
        └─► subprocess: claude --dangerously-skip-permissions -p "<prompt>"
```

The `claude_code` agent builds a rich system prompt from the context package — task
spec, acceptance criteria, validation checklist, memory extracts — then calls
`claude --dangerously-skip-permissions` and streams the result to the log.

---

### Python loop agents

Custom Python loops that call the Anthropic API directly through
`agents/shared/llm.py`. Each loop implements the tool-calling agentic loop
(read → think → act → read → …) and hits the gateway for every side effect.

| Agent ID | Module | Specialty |
|----------|--------|-----------|
| `backend-agent` | `agents.backend.main` | APIs, data models, business logic, tests |
| `frontend-agent` | `agents.frontend.main` | HTML, CSS, JS, templates |
| `qa-agent` | `agents.qa.main` | QA reports, test plans, risk assessment |

**Pros**
- Every gateway call (read, write, command) is individually audited
- Token and cost tracked per-run in Orchestra's `runs` table
- Fully deterministic tool loop — easier to debug and extend

**Cons**
- Requires `ANTHROPIC_API_KEY` in `.env`
- Smaller effective toolset than the Claude Code CLI

**How a Python agent runs**

```
Dispatcher
  └─► python -m agents.backend.main
        --context /tmp/orchestra/runs/<run_id>.json
        --run-id <uuid>
        [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
```

The agent loop in `agents/shared/loop.py`:
1. Reads the context package JSON
2. Formats it into a system prompt (task spec + memory + validation checklist)
3. Enters the tool-calling loop: calls LLM → executes tools via gateway → feeds results back
4. Continues until the model calls `task_complete` or the budget is exceeded

---

## Routing

The `owner` field on a task determines which agent type runs it:

```bash
orchctl create-task "Add login endpoint" --owner backend-agent
orchctl create-task "Add login page"     --owner frontend-agent
orchctl create-task "Write QA report"    --owner qa-agent
orchctl create-task "Implement auth"     --owner claude-code-agent
```

When using `orchctl request` or `orchctl request --spec`, the root agent picks the
owner automatically based on the nature of the work.

---

## Context package

Before an agent is launched the orchestrator assembles a context package — a JSON
file written to `RUN_STORE_DIR` that contains everything the agent needs:

```json
{
  "run_id": "...",
  "task": {
    "id": "TASK-001",
    "title": "Add login endpoint",
    "owner": "backend-agent",
    "inputs": ["spec.md"],
    "outputs": ["app/auth.py", "tests/test_auth.py"],
    "acceptance": ["POST /login returns 200 with valid credentials"],
    "validators": ["ruff", "pytest"]
  },
  "input_artifacts": {
    "spec.md": "<file content>"
  },
  "memories": {
    "identity": "...",
    "skills": [...],
    "episodes": [...]
  },
  "gateway_url": "http://localhost:8081",
  "orchestrator_url": "http://localhost:8080",
  "capability_token": "<JWT>"
}
```

The `capability_token` is a short-lived HS256 JWT that grants the agent write access
to the paths in `task.outputs` only. The gateway verifies this token on every request.

---

## Agent tools (gateway API)

Agents call the gateway via HTTP. Python loop agents receive these as Anthropic tool
definitions; claude-code-agent uses the MCP-compatible tools automatically.

| Tool | Endpoint | What it does |
|------|----------|-------------|
| `read_artifact` | `POST /read_artifact` | Read a file from the target repo |
| `write_artifact` | `POST /write_artifact` | Write a file (enforces output-path scope) |
| `run_command` | `POST /run_command` | Run a shell command in the repo |
| `emit_event` | `POST /emit_event` | Write a structured event to the control plane |
| `git_branch` | `POST /git/branch` | Create or checkout a branch |
| `git_commit` | `POST /git/commit` | Stage paths and commit |
| `memory_search` | `POST /memory/search` | Search the agent's own memories |
| `task_complete` | `POST /emit_event` (TASK_COMPLETED) | Signal task completion |
| `discover_task` | `POST /emit_event` (TASK_DISCOVERED) | Spawn a child task |

Every call is logged as an audit row in `audit_rows`. The audit trail is inspectable
via `orchctl audit TASK-001`.

---

## Heartbeat

Once an agent is running it must POST to `POST /heartbeat` every 60 seconds. The
gateway records a Redis key with a 180-second TTL. The dispatcher's heartbeat watchdog
checks all running tasks on each event loop tick. If a task's key is missing and the
run is older than 6 minutes (grace period for slow startup), the dispatcher transitions
the task to `suspended`.

Resume with: `orchctl resume TASK-001`

---

## Agent identity and memory

Each agent builds up a persistent identity across tasks. See [Memory](./memory.md) for
the full system. In brief:

- **Identity memory** — role description + accumulated domain expertise. Updated by the
  dispatcher after each task completion based on the files the agent wrote.
- **Episode memories** — one per task completion; captures what was done, branch, files.
- **Skill memories** — facts injected by humans (`orchctl teach`) or written by agents.

The context packager injects the three most relevant memories into every run's context
package. Agents can also search their own memories mid-task via `memory_search`.

---

## Adding a custom agent

1. Create `agents/myagent/main.py` with the same `--context`, `--run-id`, `--repo`,
   `--gateway-url`, `--orchestrator-url` interface.
2. Register the module in `_AGENT_MODULES` in `orchestrator/orchestrator/dispatcher.py`.
3. Use `--owner myagent` when creating tasks.

The agent must:
- Read the context package from the `--context` path
- Use the gateway URL for all side effects (never direct filesystem access)
- Include the `capability_token` from the context package in every gateway request
  as `Authorization: Bearer <token>`
- Post heartbeats every 60 s or fewer to `POST /heartbeat`
- Call `emit_event` with `TASK_COMPLETED` when done (or `TASK_FAILED` on failure)
