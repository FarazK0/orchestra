# Shared agent infrastructure

## llm.py
Single LLM client wrapper. All Anthropic API calls go through here. Records tokens and cost per call to the control plane (`runs` table). Never call the provider SDK directly from agent code.

## loop.py
Base agent loop for Python loop agents (backend-agent, frontend-agent, qa-agent). Drives a Claude tool-use conversation until `task_complete` is called.

**Gateway tools exposed to the agent:**

| Tool | What it does |
|---|---|
| `read_artifact` | Read a file from the managed repo |
| `write_artifact` | Write or overwrite a file in the managed repo |
| `run_command` | Run a command in the repo directory (e.g. `pytest`, `ruff`) |
| `emit_event` | Emit a structured event to the control plane |
| `task_complete` | Commit changed files and transition task to `completed` |
| `write_memory` | Persist a reusable skill or project convention (writes `memory_type="skill"` via gateway) |
| `search_memory` | Keyword search the agent's memory archive + shared project pool mid-task |

**Context package rendering:** `format_context_package()` renders the JSON context package as the agent's opening user message, including identity, past episodes, acquired skills, and shared project conventions if any memory exists.
