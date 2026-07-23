# Agent Memory

Orchestra gives each agent a persistent memory that accumulates across tasks and
survives service restarts. Memory is stored in the `agent_memories` Postgres table
and injected into every context package.

---

## Memory types

### Identity memory (`memory_type = "identity"`, `key = "identity"`)

One row per agent. Contains the agent's role description and accumulated domain
expertise. Updated by the dispatcher after each task completion.

Example content:
```
## Role
You are the backend specialist for this project.
You implement server-side logic, APIs, data models, and database interactions.

## Domain expertise
- Python (from TASK-001, TASK-003)
- REST APIs (from TASK-002)
- DB migrations (from TASK-004)
- Testing (from TASK-003)

## Project snapshot
<recent git log / file tree summary>
```

The dispatcher builds this automatically. You can also view and edit it:

```bash
orchctl identities                     # view all agents
orchctl identities --agent backend-agent
orchctl teach backend-agent "Always use select() not query() for SQLAlchemy 2.x" \
    --topic sqlalchemy-style
orchctl forget backend-agent sqlalchemy-style
```

### Episode memories (`memory_type = "episode"`, `key = "episode/<task-id>"`)

One row per completed task. Written by the dispatcher after `TASK_COMPLETED`.
Captures what the agent did: branch name, files changed, success/failure.

Example content:
```
Task TASK-003 (completed): Implement login endpoint
Branch: agent/backend/TASK-003
Files changed: app/auth.py, tests/test_auth.py
Outcome: success
```

Episodes give agents a sense of project history when they start a new task.

### Skill memories (`memory_type = "skill"`, `key = "skill/<topic>"`)

Reusable facts and patterns. Can be written by:
- Humans via `orchctl teach`
- Agents themselves (via `POST /memory/upsert` through the gateway)
- The root agent (during decomposition)

Example content:
```
Always use httpx.AsyncClient for async HTTP in this project.
The API base URL is read from settings.API_URL, never hardcoded.
```

Agents can also write skill memories themselves when they discover project-specific
patterns mid-task.

---

## How memory is injected

When the orchestrator assembles a context package it queries the agent's memories:

1. **Identity** — the single identity row is always included
2. **Skills** — the 3 most recently updated skill rows are included
3. **Episodes** — the 3 most recent episode rows are included

The context packager also runs a keyword search (`POST /memory/search`) to surface
memories relevant to the current task title and description.

Agents receive memories in the context package JSON under `"memories"`:
```json
{
  "memories": {
    "identity": "## Role\n...",
    "skills": ["Always use select() not query()...", "..."],
    "episodes": ["Task TASK-001 (completed)...", "..."]
  }
}
```

Python loop agents see these rendered into the system prompt as a "Memory" section.
The `claude-code-agent` sees them in the prompt passed to the `claude` subprocess.

---

## Domain expertise accumulation

After each `TASK_COMPLETED` event the dispatcher:

1. Queries the audit trail for all `gateway:write_artifact` actions on that task
2. Extracts the file paths the agent wrote
3. Maps them to domain tags using heuristic rules:

| Files written contain | Tag assigned |
|-----------------------|-------------|
| `test`, `test_` prefix | Testing |
| `api`, `route`, `endpoint` | REST APIs |
| `.html`, `.css`, `.js`, `.ts` | Frontend |
| `migration`, `alembic` | DB migrations |
| `model`, `schema` | Data models |
| `.py` | Python |

4. Merges the new tags into the agent's identity memory, preserving existing tags
   and adding task provenance (`(from TASK-003)`)

This means the longer an agent works on a project, the richer its identity becomes.
`orchctl identities` shows the accumulated expertise.

---

## Human-taught skills

You can inject knowledge directly into an agent's memory:

```bash
# Inject a skill fact
orchctl teach backend-agent \
    "This project uses PostgreSQL 16. Always use JSONB for semi-structured data." \
    --topic postgres-conventions

# List skills
orchctl memory list --agent backend-agent --type skill

# Remove a skill
orchctl forget backend-agent postgres-conventions
```

Skills injected this way are tagged `skill/human/<topic>` in the database. They are
treated differently from agent-written skills: `orchctl forget` only deletes
human-taught skills (preventing agents from being accidentally wiped).

---

## Probing agent competency

```bash
# One-shot question (no services required)
orchctl ask backend-agent "What database conventions does this project use?"

# Interactive multi-turn session
orchctl session backend-agent
```

Both commands load the agent's full identity and skill memories and pose the question
to an LLM, giving you a way to verify what the agent "knows" before assigning it a task.

The LLM backend is configured separately:
```bash
orchctl config set llm-backend claude    # use the claude CLI (default)
orchctl config set llm-backend python    # use LLMClient + ANTHROPIC_API_KEY
orchctl config show
```

---

## Safety valves

The memory system is fully inspectable and editable by humans:

```bash
orchctl memory list                          # all memories
orchctl memory list --agent backend-agent    # one agent
orchctl memory list --type skill             # by type
orchctl memory show <memory-id>              # full content
orchctl memory delete <memory-id> --yes      # delete with audit record
```

Deletion writes an audit row before removing the memory row, so the action is
traceable even after the memory is gone.

---

## Memory storage limits

- Content cap: 2000 characters per memory row (enforced by the gateway on upsert)
- Skill deduplication: if an agent writes a skill with the same topic as an existing
  one, the content is merged rather than creating a duplicate row
- The `agent_memories` table key is `(agent_id, project_id, key)` — each
  `(agent, project, key)` triple holds exactly one row; upsert replaces on conflict
