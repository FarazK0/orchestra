# arch-to-tasks

Convert an architecture or specification document into an Orchestra task plan JSON file.

## Usage

```
/arch-to-tasks <path-to-spec-or-architecture-file>
```

Example: `/arch-to-tasks diary_spec.md`

## What this skill does

1. Reads the document at the path given in `$ARGUMENTS`.
2. Analyses the content and decomposes the work into 3–5 tasks for Orchestra's specialist agents.
3. Writes the task plan as JSON to `tasks.json` in the current working directory.
4. Prints a summary and the commands to review and submit the plan.

## Instructions

Read the file at the path provided in `$ARGUMENTS`. If no path is given, tell the user to provide one and stop.

Analyse the document carefully and produce a task plan following these rules:

**Agents available:**
- `backend-agent` — server-side code: APIs, data models, business logic, database, tests
- `frontend-agent` — client-side code: HTML, CSS, JavaScript, single-page UI, browser interaction
- `qa-agent` — quality assurance: test plans, QA reports, edge-case analysis, risk assessment
- `claude-code-agent` — general purpose: Claude Code CLI as the worker; suitable when a single capable agent can handle the full implementation without specialist separation

**Task plan rules:**
- Keep the plan to 3–5 tasks. Do not create tasks for work an agent can handle internally.
- `backend-agent` tasks have no `depends_on` — they are always roots.
- `frontend-agent` and `qa-agent` tasks list the backend task title(s) in `depends_on` when they consume backend outputs.
- `inputs` are files the agent reads (must already exist or be produced by a dependency).
- `outputs` are files the agent writes (repo-relative paths).
- `acceptance` is a list of testable, behaviour-focused criteria. Do NOT mention specific
  tools (ruff, pytest, etc.) — validators are auto-detected from output file extensions
  and configured separately. Focus on what the code must do, not how it is checked.

**Output format** — a JSON array, one object per task:
```json
[
  {
    "title":      "<short imperative phrase, e.g. 'Implement items API with CRUD endpoints'>",
    "owner":      "backend-agent",
    "depends_on": [],
    "inputs":     ["<spec-file-relative-to-repo>"],
    "outputs":    ["app/main.py", "tests/test_app.py"],
    "acceptance": [
      "GET /items returns a JSON array of all items",
      "POST /items creates a new item and returns 201 with the created object"
    ]
  },
  {
    "title":      "<frontend task title>",
    "owner":      "frontend-agent",
    "depends_on": ["<exact title of the backend task above>"],
    "inputs":     ["app/main.py"],
    "outputs":    ["frontend/index.html"],
    "acceptance": ["Page loads without console errors and all list items render"]
  }
]
```

Omit the `validators` field — the orchestrator auto-detects appropriate validators from
the output file extensions (ruff + pytest for `.py` files, eslint + jest for `.ts/.js`, etc.).

Determine the output path for the plan JSON:
- If `SANDBOX_REPO_PATH` is set in the environment, write to `$SANDBOX_REPO_PATH/tasks.json`
- Otherwise write to `tasks.json` in the current working directory

After writing, print:
- A one-line summary for each task (ID not yet known — just title and owner).
- The exact commands to review and submit the plan:

```
Review the plan:
  cat tasks.json

Easiest submit — let the root agent handle routing and dispatch:
  orchctl request --spec <spec-file-path>

Or submit the pre-built plan directly (services must be running):
  uv run python -m agents.planner.main \
      --plan tasks.json \
      --repo $SANDBOX_REPO_PATH
```

Do not call any external APIs. Use only the Read and Write tools for file I/O.
