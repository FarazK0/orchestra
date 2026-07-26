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

## Not yet fixed

### 4. Agent-launch env_limitation not routed to `awaiting_human`

**File:** `agents/claude_code/main.py` (failure branch, ~line 550)  
**Symptom:** When the Claude CLI exits with code 1 and stdout contains
`"You've hit your session limit"`, the claude_code agent falls through to the generic
failure path: `running → failed`. After three retries the task escalates. This is wrong:
a session-limit error is an `env_limitation` (external resource unavailable), not a task
defect. It should route to `awaiting_human` exactly like the validator's env_limitation
path does for install failures.  
**Proposed fix:** In the claude_code agent's error handler, after a non-zero exit, parse
the combined stdout/stderr for patterns indicating environment problems (session limit,
rate limit, authentication failure). On match, call the `human_input/request` endpoint
with `question_type: "env_limitation"` and the raw error text, then exit 0. The task
transitions `running → awaiting_human` instead of `running → failed`. Human resumes
via `orchctl respond` once the limit resets.  
**Workaround used:** Manually transitioned `escalated → assigned` after limit reset.

---

### 5. Dependent tasks start from `main`, not from their dependency's branch

**Files:** Dispatcher / agent runner (branch creation logic)  
**Symptom:** TASK-003 (`server.py`) and TASK-002 (`golden fixtures`) both depended on
TASK-001 (`local storage backend`). The dependency check only verifies task *status*
(`completed` or later). Worktrees are always created from `HEAD` of `main`. Since
TASK-001 was `completed` but not yet *merged to main*, TASK-002 and TASK-003 ran on
the old codebase without the local storage backend. TASK-003 re-implemented `store.py`
from scratch (wrong file, wrong task) before hitting the session limit.  
**Proposed fix (option A):** Block dependent tasks from dispatching until all `depends_on`
tasks are `merged` (not just `completed`). Adjust `get_ready_successors` in `dag.py`
to require `merged` or `closed` status.  
**Proposed fix (option B):** When creating a worktree for a task with `depends_on`, merge
the upstream agent branches into the worktree before handing it to the agent. More complex
but allows true parallel pipelines.  
**Workaround used:** Merged TASK-001 before re-queuing TASK-002 and TASK-003.

---

### 6. TASK_DISCOVERED with empty payload leaves parent stuck at `running`

**File:** `orchestrator/orchestrator/scheduler.py:54` (`handle_task_discovered`)  
**Symptom:** TASK-001's agent emitted `TASK_DISCOVERED` with payload `{}`. The scheduler
returned `None` (no child task created, no transition on the parent). The agent process
had already exited (exit 0). TASK-001 was left at `running` with no active agent and no
dispatcher event to advance it.  
**What should happen:** If `handle_task_discovered` returns `None` because the payload is
malformed (no title, no outputs), the dispatcher should either (a) log a warning and
transition the parent to `failed` so the retry logic kicks in, or (b) emit an audit event
and revert the parent to `assigned` for a clean restart.  
**Proposed fix:** In `dispatcher._on_task_discovered`, if `child is None`, transition the
parent task from `running` to `failed` with `failure_reason="task_discovery_rejected"` so
the existing retry/escalation path handles it rather than leaving the task orphaned.  
**Workaround used:** Manually transitioned TASK-001 `running → completed` via the API,
with a note explaining that the implementation was complete and the TASK_DISCOVERED was
erroneous.

---

### 7. Root agent `too_many_outputs` threshold blocks tasks with many fixtures

**File:** `agents/root/main.py` (oversized check, `len(outputs) > 2`)  
**Symptom:** TASK-002 has 12 output files (5 `.npy` input tensors, 5 `.json` golden
fixtures, `tests/test_golden.py`, `tests/__init__.py`). The root agent's guard
`too_many_outputs = len(outputs) > 2` held the entire plan in `created` state waiting for
human review before any task could be assigned. This is correct behaviour in principle
(large fan-out warrants human review) but the threshold of 2 is far too low for
fixture-generating tasks.  
**Proposed fix:** Raise the threshold (e.g. `> 10`) or make it configurable via an
environment variable (`ORCHESTRA_MAX_OUTPUTS_BEFORE_REVIEW`). Alternatively, exclude
test fixtures (paths under `tests/fixtures/`) from the output count since they are
generated data, not production code changes.  
**Workaround used:** Manually approved all three tasks with `orchctl approve`.
