# Agents

Orchestra uses a two-layer model for agent execution. Understanding this distinction
avoids confusion when reading task output or choosing agent identities.

---

## Two-layer model

**Layer 1 — Execution backend** (set once at platform level via `AGENT_TYPE`):

| `AGENT_TYPE` | What runs | Credential needed |
|---|---|---|
| `claude-code` (default) | `agents.claude_code.main` — launches the `claude` CLI subprocess | `claude login` (no API key) |
| `python` | Identity-specific Python loop per task | `ANTHROPIC_API_KEY` in `.env` |

**Layer 2 — Agent identity** (set per task via `task.owner`):

Determines the role description, domain expertise, and skill memories injected into
whichever backend runs. The identity travels with the task, not with the platform.

Recommended identities and their specialisations:

| Identity | Specialises in |
|---|---|
| `backend-agent` | APIs, data models, business logic, migrations, server tests |
| `frontend-agent` | HTML, CSS, JS, templates, browser interaction |
| `qa-agent` | Test plans, QA reports, risk assessment (no implementation) |
| *(any string)* | Arbitrary specialisation — the identity system accepts any value and seeds context from accumulated memories |

**These two concerns are independent.** `AGENT_TYPE` never changes per task. `task.owner`
never changes which execution backend runs.

### Dispatch table

| `task.owner` | `AGENT_TYPE=claude-code` (default) | `AGENT_TYPE=python` |
|---|---|---|
| `backend-agent` | `agents.claude_code.main` (backend identity) | `agents.backend.main` |
| `frontend-agent` | `agents.claude_code.main` (frontend identity) | `agents.frontend.main` |
| `qa-agent` | `agents.claude_code.main` (QA identity) | `agents.qa.main` |
| *(any other string)* | `agents.claude_code.main` (identity from memory) | `agents.backend.main`† |
| `claude-code-agent`‡ | `agents.claude_code.main` (no domain identity) | `agents.claude_code.main` |

†Unknown identities in python mode fall back to `agents.backend.main` as a placeholder.
A generic Python agent loop that handles arbitrary identities is future work (shelved).

‡`claude-code-agent` is a technical escape hatch — it always routes to the claude CLI
and injects no domain specialisation. Do not use it as a task identity in new plans;
assign a domain identity instead.

---

## Execution backends

### Claude Code backend (default)

Launched when `AGENT_TYPE=claude-code` (or unset). The `agents.claude_code.main` module
builds a rich system prompt from the context package and runs:

```
Dispatcher
  └─► python -m agents.claude_code.main
        --context /tmp/orchestra/runs/<run_id>.json
        --run-id <uuid>
        [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
        └─► subprocess: claude --dangerously-skip-permissions -p "<prompt>"
```

**Pros**
- No `ANTHROPIC_API_KEY` needed in `.env`
- Uses the same `claude` session you are already authenticated with
- Full Claude Code toolset available inside the agent (web search, MCP, etc.)

**Cons**
- Individual file writes are not separately audited through the gateway (branch creation
  and git commit still go through the gateway)
- Token/cost accounting is in the Claude Code billing dashboard, not in Orchestra's `runs` table

---

### Python loop backend

Launched when `AGENT_TYPE=python`. Custom Python loops that call the Anthropic API
directly through `agents/shared/llm.py`.

```
Dispatcher
  └─► python -m agents.backend.main   (or frontend.main / qa.main)
        --context /tmp/orchestra/runs/<run_id>.json
        --run-id <uuid>
        [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
```

The agent loop in `agents/shared/loop.py`:
1. Reads the context package JSON
2. Formats it into a system prompt (task spec + memory + validation checklist)
3. Enters the tool-calling loop: calls LLM → executes tools via gateway → feeds results back
4. Continues until the model calls `task_complete` or the budget is exceeded

**Pros**
- Every gateway call (read, write, command) is individually audited
- Token and cost tracked per-run in Orchestra's `runs` table
- Fully deterministic tool loop — easier to debug and extend

**Cons**
- Requires `ANTHROPIC_API_KEY` in `.env`
- Smaller effective toolset than the Claude Code CLI

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
definitions; the claude-code backend has gateway tools available through MCP.

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
