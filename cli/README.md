# orchctl

Typer-based CLI, the human interface to the orchestrator.

Run all commands via `uv run python -m cli.main <command>`.

## Commands

### Change requests (Phase 3)
- `request "description" [--spec PATH]` -- submit a change request to the root agent; decomposes into tasks and dispatches automatically

### Task management
- `create-task TITLE [--owner AGENT_ID] [--accept CRITERION] [--input PATH] [--output PATH] [--depends-on TASK-ID]` -- create a task manually
- `list [--status STATUS]` -- list tasks
- `approve TASK-ID` -- advance through human approval gate (created->assigned, validated->merged)
- `cancel TASK-ID [--reason TEXT]` -- cancel a task from any non-terminal state
- `run-task TASK-ID --repo PATH` -- assemble context package and start run
- `validate TASK-ID --repo PATH` -- run ruff + pytest on agent branch
- `merge TASK-ID --repo PATH` -- merge agent branch into main via gateway
- `review --repo PATH` -- interactive approval loop: auto-validates completed tasks, shows results, prompts for merge or cancel

### Agent memory (Phase 3)
- `memory list [--agent AGENT_ID] [--type TYPE] [--project PROJECT]` -- list memory entries with last-used staleness column
- `memory show MEMORY_ID [--agent AGENT_ID]` -- show full content of one memory row (accepts 8-char UUID prefix)
- `memory delete MEMORY_ID [--agent AGENT_ID] [--reason TEXT] [--yes]` -- delete a memory row and write an audit record

Valid `--owner` / `--agent` values: `backend-agent`, `frontend-agent`, `qa-agent`, `claude-code-agent`, `shared` (shared project pool).
