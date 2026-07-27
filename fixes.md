# Issues and fixes discovered during phase0-local-runnable spec run

Logged: 2026-07-26. Change request: "Complete phase 0 — make gapo locally runnable without AWS."

---

## Fixed this session

### 4. LLM acceptance validator hallucinate about existing repo files

**File:** `orchestrator/orchestrator/validator.py` (`_check_llm_acceptance`)  
**Symptom:** TASK-003's llm-acceptance check failed with verdicts like "import handler —
nowhere in the codebase" and "web/index.html does not exist." Both files DO exist on main.
The LLM was only given the git diff, not the full repo file listing, so it guessed that
anything referenced in the diff but not in the diff itself was missing.  
**Fix:** Added a `## Existing files in the repository (main branch)` section to the
`_LLM_ACCEPTANCE_PROMPT`, populated by `git ls-files --with-tree=main` (capped at 120
lines). The LLM now knows what's already in the repo before the agent's changes were
applied.  
**Root cause:** The prompt was designed around the diff alone. Prompts that describe
running behavior (imports, file reads) need to know what already exists in the repo.

---

### 5. Dispatcher crashes on Redis socket timeout in WSL2

**File:** `orchestrator/orchestrator/streams.py` (`StreamConsumer.consume_one`)  
**Symptom:** The dispatcher logged `redis.exceptions.TimeoutError: Timeout reading from
socket` and appeared to terminate its event loop. The WSL2 virtualized network stack
fires socket idle-timeouts before the `block_ms=1000` window expires on blocking
`XREADGROUP` calls. This was not caught — only `ResponseError` was handled — and the
uncaught exception propagated up through `dispatcher.start()`'s `while True:` loop.  
**Fix:** Added `except redis.exceptions.TimeoutError: return False` in `consume_one`,
treating a socket timeout the same as an empty result (no new events). The dispatcher
continues normally on the next iteration.  
**Root cause:** Missing exception handler for a known WSL2/Redis interaction.

---

### 6. Validator cannot install torch via `uv pip` (per-package `--index-url` unsupported)

**File:** `orchestrator/orchestrator/validator.py` (`_check_pytest`)  
**Symptom:** TASK-002's pytest check failed with `ModuleNotFoundError: No module named
'torch'`. The validator tried `uv pip install -r requirements-dev.txt`, but `requirements-dev.txt`
contains `torch>=2.0.0 --index-url https://download.pytorch.org/whl/cpu`. `uv pip` does
not support per-package `--index-url` options in requirements files (pip does). The fallback
`sys.executable -m pip` also failed because the orchestrator venv has no `pip` module.  
**Fix:** Replaced the "install into orchestrator venv" approach with an isolated validation
venv (`_ensure_validation_venv`). For non-pyproject repos, the validator now creates a temp
venv using the system `python3` (which always has pip), installs pytest as a baseline, then
installs all requirements files using `pip install -r {req}` (which handles all formats
including per-package options). Venvs are cached in `/tmp/orchestra/validation-venvs/` keyed
by a SHA256 hash of the requirements file contents.  
**Root cause:** The old approach polluted the orchestrator venv and depended on `uv pip`
which has a subset of pip's requirements-file feature set.

---

### 1. Context packager crashes on binary input files

**File:** `orchestrator/orchestrator/context_packager.py:52` (`_read_file`)  
**Commit:** `bc8a11b`  
**Symptom:** Dispatcher crashed building the context package for any task whose `inputs`
list contained a binary file (e.g. `protraderbot/InferenceApplication/full_model.pth`).
`path.read_text(encoding="utf-8")` raised `UnicodeDecodeError: 'utf-8' codec can't decode
byte 0x80...` which propagated unhandled and left the task stuck at `running` with no
agent process.  
**Fix:** Catch `UnicodeDecodeError` (and `ValueError`) in `_read_file`; return a
placeholder string `[binary file, N bytes, not included in context]` so the task's context
package is still built and the agent at least knows the file exists.  
**Root cause:** The root agent included a `.pth` checkpoint file in a task's `inputs` list.
Even if the agent hadn't, any binary asset (image, compiled artifact, model weights) would
cause the same crash. The fix makes context packaging robust to any input path.

---

### 2. Ruff ANSI colour codes break pre-existing soft-pass detection

**File:** `orchestrator/orchestrator/validator.py:404` (`_check_ruff`)  
**Commit:** `bc8a11b`  
**Symptom:** `test_check_ruff_pre_existing_soft_pass` failed. The validator's concise-format
ruff output contained ANSI escape codes (`\x1b[1m`, `\x1b[36m`, etc.) even with
`--output-format=concise`. The `_RUFF_CONCISE_RE` regex (`^([^:\s][^:]*?):\d+:\d+:`) could
not match a line like `\x1b[1mlegacy.py\x1b[0m\x1b[36m:\x1b[0m1:8:...`, so `error_files`
stayed empty, `untouched` was empty, and the soft-pass branch was never taken. The validator
fell through to a hard fail even though all errors were pre-existing.  
**Fix:** Pass `--color never` to the ruff invocation.  
**Root cause:** Some terminal environments (TTY detection inside the subprocess) cause ruff
to emit colours even in non-interactive mode when `--output-format=concise` is set. The
flag was missing.

---

### 3. `escalated → assigned` transition missing from state machine

**File:** `orchestrator/orchestrator/state_machine.py:36`  
**Commit:** `bc8a11b` (state_machine change included in same batch)  
**Symptom:** After a task escalated (3 consecutive failures), there was no clean path to
re-queue it without cancelling the task and losing its history. The only transitions out of
`escalated` were `→ running` (TASK_RESET, but the dispatcher has no handler for this event
so the task would sit at `running` with no agent), `→ completed` (TASK_RECOVER, semantically
wrong), and `→ cancelled`.  
**Fix:** Added `("escalated", "assigned"): "TASK_ASSIGNED"` to `TRANSITIONS`. This reuses
the TASK_ASSIGNED event type that the dispatcher already handles via `_on_task_assigned`,
so a human can transition an escalated task straight back to the dispatcher queue with no
special-case code.  
**Root cause:** The original state machine was designed before session-limit escalation was
observed in practice. Escalation was assumed to represent a permanently broken task; the
session-limit failure mode requires a simple re-queue.

---

## Fixed in second session (2026-07-27)

### 4. Agent-launch env_limitation not routed to `awaiting_human`

**File:** `agents/claude_code/main.py`  
**Fix:** Added `_ENV_LIMIT_PATTERNS` module-level list. In the `returncode != 0` branch,
before calling `_mark_suspended`, combined stdout+stderr is matched against these patterns.
On match, POST to `{gw_url}/human_input/request` with `question_type: "env_limitation"` and
exit 0. Task transitions `running → awaiting_human` via the gateway. No retry budget is
consumed.

---

### 5. Dependent tasks start from `main`, not from their dependency's branch

**File:** `orchestrator/orchestrator/dag.py`  
**Fix:** Removed `"completed"` and `"validated"` from `TERMINAL_STATUSES`. Only `"merged"`
and `"closed"` now allow successors to dispatch. Updated all existing tests in `test_dag.py`
and `test_dispatcher.py` to use `"merged"` for dependency tasks, and added three new
regression tests asserting the old (incorrect) behaviour no longer holds.

---

### 6. TASK_DISCOVERED with empty payload leaves parent stuck at `running`

**File:** `orchestrator/orchestrator/dispatcher.py` (`_on_task_discovered`)  
**Fix:** After `child = self._scheduler.handle_task_discovered(...)`, if `child is None`
and the parent task is still `running`, transitions parent to `failed` with
`failure_reason="task_discovery_rejected"` and publishes `TASK_FAILED` to Redis so the
existing retry/escalation path kicks in. Test added in `test_dispatcher.py`.

---

### 7. Root agent auto-splits oversized tasks instead of blocking for human review

**Files:** `agents/root/main.py`  
**Fix:** Replaced the `too_many_outputs` human-approval gate with recursive binary
auto-split. New helpers `_split_task` and `_expand_plan` use the LLM to split any task
with `> _MAX_OUTPUTS` outputs into exactly two semantically coherent sub-tasks, setting
`depends_on` between them if the LLM determines one group needs the other's outputs.
Recursion depth capped at 4. `_MAX_OUTPUTS` defaults to 5, overridable via
`ORCHESTRA_MAX_OUTPUTS_BEFORE_REVIEW` env var. The acceptance-criteria gate (`> 15`)
and broad-directory gate are retained as human-approval triggers. Documented in
`.env.example`.

---

### 8. `no_files_changed` false positive when prior run already committed the work

**File:** `agents/claude_code/main.py`  
**Fix:** Before marking `failed: no_files_changed`, runs `git rev-list main..HEAD --count`.
If prior commits exist on the agent branch, collects `changed_paths` from
`git diff --name-only main..HEAD`, skips the gateway commit call, reads `sha` from
`git rev-parse HEAD`, then proceeds through the normal audit-emit and completed-transition
path. Only marks failed if there are truly zero agent commits AND no current changes.
