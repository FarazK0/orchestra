# Validators

Orchestra's validator registry makes quality checks pluggable. Instead of hardcoding
a linter and test runner, each task carries a list of named validators that run after
the agent finishes. Results are shown per-check with timing and output.

---

## How validation works

1. Agent calls `task_complete` → task transitions to `completed`
2. Human (or `orchctl review`) runs `orchctl validate TASK-001 --repo PATH`
3. The orchestrator checks out the agent's branch
4. Each assigned validator runs in order, plus any `always_run` validators
5. Full per-check results are stored in `audit_rows.details`
6. On pass: `completed → validated`; on any fail: `completed → failed`

```
orchctl validate TASK-001 --repo /path/to/your-project

  Validation: PASSED  (4/4 checks)

    ✓ file-exists     All 3 output files present                  (0.1s)
    ✓ ruff            All checks passed                           (1.2s)
    ✓ pytest          47 passed in 12.3s                          (12.3s)
    ✓ llm-acceptance  3/3 criteria met                            (5.1s)
```

Validation history (the full per-check output) is also accessible via:
```bash
orchctl show TASK-001            # includes most recent validation result
GET /tasks/{id}/validation       # raw JSON via the orchestrator API
```

---

## Built-in validators

Two built-in validators run outside the normal assignment system:

### `file-exists`

Always runs. Verifies that every path declared in `task.outputs` exists on the agent's
branch after checkout. Fails if any declared output is missing.

This is the baseline correctness check: if the agent forgot to create a file it said
it would create, the task fails immediately before any other checks run.

### `llm-acceptance`

Runs only when the task has non-empty `acceptance` criteria. Uses the `claude` CLI
to evaluate each acceptance criterion against the agent's output files.

The model returns structured JSON:
```json
{"criteria": [{"text": "...", "verdict": "pass|warn|fail", "reason": "..."}]}
```

- `pass` and `warn` both count as passing for the overall verdict
- Any `fail` fails the entire check
- If the `claude` CLI is unavailable the check is skipped with a warning (soft fail)

**Gameability note**: an agent that knows its acceptance criteria can technically write
output that satisfies the wording without doing real work. The human merge gate is the
primary guard. `llm-acceptance` adds semantic coverage but is not a security boundary.

---

## Registry validators

These run only when explicitly assigned to a task (either auto-detected or manually
selected at task creation).

### `ruff`

```yaml
command: "ruff check ."
match_extensions: [".py"]
```

Runs `ruff check .` in the repo root. Auto-detected when any output path ends in `.py`.
Fails on any lint error.

### `pytest`

```yaml
command: "pytest --tb=short -q"
match_extensions: [".py"]
match_paths: ["tests/", "test_"]
```

Runs `pytest --tb=short -q` in the repo root. Auto-detected when any output path ends
in `.py` and a `tests/` directory or `test_` file exists. Fails on any test failure.

### `mypy`

```yaml
command: "mypy ."
match_extensions: [".py"]
auto_detect: false
```

Opt-in only — not auto-detected. Add it manually at task creation. Runs `mypy .` and
fails on any type error.

### `eslint`

```yaml
command: "npx eslint ."
match_extensions: [".js", ".ts", ".tsx", ".jsx"]
```

Auto-detected when any output path is a JavaScript or TypeScript file.
Requires `npx` and an ESLint config in the repo.

### `jest`

```yaml
command: "npx jest --passWithNoTests"
match_extensions: [".test.js", ".spec.js", ".test.ts", ".spec.ts", ".test.tsx"]
```

Auto-detected when any output path is a test file. `--passWithNoTests` means a repo
with no tests yet does not fail.

---

## Validator assignment

### Auto-detection

When a task is created, the orchestrator inspects `task.outputs` and selects validators
whose `match_extensions` or `match_paths` match any output file. Validators with
`auto_detect: false` (e.g. `mypy`) are never auto-detected.

`always_run` validators (`file-exists`, `llm-acceptance`) are not part of auto-detection;
they run unconditionally based on their own conditions.

### Interactive selection at task creation

```bash
orchctl create-task "Add auth endpoint" --owner backend-agent \
    --output app/auth.py --output tests/test_auth.py
```

Output:
```
  Detected validators for this task:
    ✓ file-exists     — Verify every output file exists (always runs)
    ✓ ruff            — Python linter
    ✓ pytest          — Python test runner
    ✓ llm-acceptance  — LLM evaluates acceptance criteria (always runs if criteria set)

  Accept? [Y/n/edit]:
```

Type `edit` to enter edit mode:
- `+mypy` to add a validator
- `-pytest` to remove one
- `+custom:python smoke.py` to add a one-off shell command

### Planner-created tasks

Tasks created by the root agent via `orchctl request` skip interactive selection.
The orchestrator auto-detects validators from the task's declared outputs.

### Backward compatibility

Tasks created before the validator registry existed have `validators = []`. At
validation time the orchestrator detects validators from `task.outputs` automatically,
so old tasks still validate correctly.

---

## Adding a custom validator

Edit `permissions/validators.yaml`:

```yaml
  api-smoke-test:
    description: "Hit the /health endpoint and verify 200"
    command: "python scripts/smoke_test.py"
    match_extensions: [".py"]
    match_paths: ["app/"]
```

Fields:
- `description` — shown in `orchctl validator list` and the task creation prompt
- `command` — shell command run in the repo root; exit 0 = pass, non-zero = fail
- `match_extensions` — file extensions that trigger auto-detection
- `match_paths` — path fragments that trigger auto-detection (prefix or substring match)
- `auto_detect` — set `false` to make it opt-in only (default: `true`)
- `always_run` — set `true` to run regardless of assignment (reserved for built-ins)

No restart required — the registry is read from disk at validation time.

---

## Viewing the registry

```bash
orchctl validator list
```

```
  Available validators  (permissions/validators.yaml)

  NAME              AUTO  DESCRIPTION
  file-exists       yes   Verify every output file declared on the task exists
  ruff              yes   Python linter (ruff check .)
  pytest            yes   Python test runner (pytest --tb=short -q)
  mypy              opt   Python static type checker (mypy .)
  eslint            yes   JavaScript/TypeScript linter (npx eslint .)
  jest              yes   JavaScript/TypeScript test runner (npx jest)
  llm-acceptance    yes   LLM evaluates each acceptance criterion
```

The orchestrator API also exposes the registry at `GET /validators`.
