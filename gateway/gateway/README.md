# Tool Gateway

The ONLY path to side effects for agents. Port 8081.

Every endpoint: (1) verifies caller identity (`(agent_id, task_id)` active run check or `X-Platform-Actor` header for platform writes), (2) executes the operation, (3) writes an Event + AuditRow atomically with the side effect.

## Endpoints

### Artifact and command operations
- `POST /read_artifact` -- read a file from the managed repo (audited)
- `POST /write_artifact` -- write a file to the managed repo (audited)
- `POST /run_command` -- run a command in the repo directory; subprocess on host (Docker sandbox in a later phase) (audited)
- `POST /emit_event` -- write an event to the control plane (audited)

### Git operations
- `POST /git/branch` -- create or checkout a branch (audited)
- `POST /git/commit` -- stage paths and commit (audited)
- `POST /git/merge` -- merge agent branch into target branch; requires `validated` task status (human gate) (audited)

### Agent memory operations
- `POST /memory/upsert` -- write or update an agent memory entry (audited)
  - Platform writes (dispatcher, root-agent): supply `X-Platform-Actor` header; `agent_id` taken from body; any `memory_type` allowed
  - Agent writes: `agent_id` derived from `tasks.owner` for the running task; only `memory_type="skill"` accepted; content capped at 2000 chars; same-topic skill rows are deduplicated
- `POST /memory/search` -- keyword (ILIKE) search over agent's own memories + shared pool (`agent_id="shared"`); derives caller identity from running task; returns up to `max_results` snippets (audited)

## Trust model

- Regular agents: must supply `task_id` of an active run; `agent_id` is always derived from `tasks.owner`, never taken from the request body.
- Platform actors (`dispatcher`, `root-agent`): supply `X-Platform-Actor` header; bypass the running-task check; supply `agent_id` in body (trusted).
- Memory type guard: agents cannot write `identity` or `episode` memories -- those are platform-only. Agents write `skill`; root-agent writes `identity` and `convention` (shared pool); dispatcher writes `episode`.
