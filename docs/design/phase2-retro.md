# Phase 2 Retrospective

**Scope completed:** Concurrency -- Redis Streams event bus, DAG scheduling, multi-agent fan-out, retry, Tier 0 auto-merge, Claude Code as agent worker, interactive review loop.
**Date:** 2026-07-16

---

## What shipped

All nine Phase 2 steps shipped, plus three significant unplanned additions:

**Planned (Steps 14-22):**

14. Redis Streams event bus -- one stream per project, consumer groups per agent type, `event_id` dedup table, pending-entry reclaim on restart
15. DAG scheduling and event-driven dispatch -- replaced direct orchestrator-to-agent calls; tasks with `depends_on` only dispatch when all upstreams are in `closed` status
16. Frontend agent with its own system prompt and gateway scope
17. QA agent with pytest-driven acceptance checking; dispatcher routes by `task.owner`
18. Retry policy with fresh-branch semantics -- failed tasks re-enqueue on a new branch `agent/{type}/{task_id}-retry-N`; escalation after max retries
19. Tier 0 auto-merge -- tasks that pass `ruff check` + `pytest` with no human-authored file modifications merge automatically on `TASK_VALIDATED`
20. Multi-agent fan-out -- `orchctl create-task --depends-on` wires the DAG; `demo_v2.sh` runs three concurrent tasks with a dependency chain
21. End-to-end demo v2 -- fixed agent-branch derivation (`owner.removesuffix("-agent")` pattern) so all three agent types validate and merge cleanly
22. Phase 2 retro (this document)

**Unplanned additions (emerged from using the system):**

- **Claude Code as agent worker** (`agents/claude_code/main.py`) -- instead of maintaining three custom LLM loops, the system now launches `claude --dangerously-skip-permissions -p` as a subprocess; branch creation and commit still go through the gateway; individual writes are not individually audited (Phase 3 revisit)
- **Setup script + planner agent** (`scripts/setup.sh`, `agents/planner/main.py`) -- one-command onboarding: reads a spec file, decomposes it into tasks via the planner, submits them, then drops into the review loop
- **Interactive review loop** (`orchctl review`) -- polls for `completed`/`validated` tasks, auto-validates each, shows ruff/pytest results inline, and prompts for approve-or-skip rather than requiring the user to run three separate commands per task

**Test count:** 193 passing tests.

---

## What hurt

### 1. Agent-branch naming was silently wrong

The dispatcher derived the branch prefix from `task.owner` literally (`backend-agent`, `frontend-agent`, `qa-agent`), giving branches named `agent/backend-agent/TASK-001`. The validator and merge flow expected `agent/backend/TASK-001`. This mismatch meant every Step 16-17 agent task completed but never validated: the validator checked out the wrong (nonexistent) branch and reported "nothing to validate". The fix (`owner.removesuffix("-agent")`) is one line, but it cost the end-to-end demo until Step 21.

**Lesson:** Integration tests that cover the full create-dispatch-validate-merge chain are more valuable than unit tests for the dispatcher in isolation. The branch-naming contract spans three components (dispatcher, validator, gateway) and none of their unit tests caught the mismatch.

### 2. `__pycache__` artifacts blocked merges

Agents (and later the validator running pytest) left `*.pyc` files in the working tree. Because the sandbox repo had no `.gitignore`, some of these ended up committed to agent branches. When the gateway tried to `git checkout main` before merging, git refused because main had the same tracked pyc files at different SHAs. This hit on every merge in the demo.

Three-part fix: add `.gitignore` to the sandbox repo, filter pyc from the agent commit, and `git checkout -- . && git clean -fd` in the gateway merge endpoint before the branch switch.

**Lesson:** The sandbox demo repo needs to be treated as a first-class artifact, not a throwaway. Its `.gitignore` should have been part of the initial scaffold.

### 3. ANTHROPIC_API_KEY vs claude CLI auth conflict

When `ANTHROPIC_API_KEY` is present in the environment (even as an empty string), the `claude` CLI interprets it as a request to use API-key auth rather than its own session auth, prints a warning, and may fail. The setup script originally asked for the key upfront for all flows; the `claude` subprocess then saw the empty variable.

Fix: filter `ANTHROPIC_API_KEY` out of the environment before any `claude` subprocess call, and restructure setup.sh to only prompt for the key when Python loop agents are chosen.

**Lesson:** Environment variable inheritance is a hidden interface. Any subprocess that inspects env vars needs explicit documentation of which variables it reacts to.

### 4. Silent progress during claude CLI spec generation

`setup.sh` piped the claude CLI's stdout to a file and showed no progress indicator. From the user's perspective, the terminal appeared to freeze for 60-90 seconds. Added a timing note ("Usually takes 30-90 seconds...") and `</dev/null` to close stdin so the process can't block on input.

**Lesson:** Long-running silent subprocesses need at minimum a "this will take ~N seconds" message. Even a spinner would be better.

### 5. Step numbering vs actual sequence

The design doc's Phase 2 steps 16-19 described DAG + concurrency guard + multi-agent + Q&A flow. What actually shipped in those step slots was frontend agent, QA agent, retry, and Tier 0 auto-merge -- a different ordering. The concurrency guard (overlapping output detection) was not built; the Q&A event flow was deferred. This is fine, but it means the step numbers in commit history do not map to design-doc step numbers.

**Lesson:** Keep the design doc as intent, not a changelog. Actual sequence belongs in git history and retros.

---

## What worked well

- **Redis Streams dedup** held correctly across all demo runs. Killing and restarting the dispatcher during a run always recovered without duplicate agent launches.
- **DAG fan-out** worked on the first attempt once the branch-naming bug was fixed. The `depends_on` topological sort is simple (BFS readiness check) but sufficient for the demo's linear chains.
- **Claude Code as agent** dramatically reduced the maintenance surface. Three custom LLM loops would have needed ongoing prompt tuning; `claude` already knows how to read files, write code, and check its own output. The tradeoff (no per-write audit) is acceptable at this phase.
- **Tier 0 auto-merge** removed the human from the happy path for clean tasks. The review loop now only surfaces tasks that need a decision.
- **The `orchctl review` loop** made the human experience feel complete for the first time. Instead of running five CLI commands to close one task, the human reads results and presses `a`.
- **Two-plane discipline** continued to hold. Zero temptation to put artifact content in Postgres during Phase 2, even when it would have been shorter.

---

## What was not built (deferred)

- **Concurrency guard** (overlapping output path detection at scheduling time) -- skipped; no two demo tasks write the same paths in practice
- **Q&A event flow between agents** -- deferred to Phase 3; the claude-code-agent handles ambiguity internally
- **Per-write gateway audit for claude-code-agent** -- claude writes files directly; only branch creation and the final commit are audited; Phase 3 revisit
- **Docker sandbox for run_command** -- still subprocess on host; Phase 3

---

## Phase 3 priorities (in order)

1. **Persistent root agent** -- a long-running process that accepts change requests, decomposes them via the planner, and dispatches sub-agents; makes the workflow ongoing rather than one-shot
2. **Capability tokens** -- PASETO/JWT minted at assignment, verified by gateway, scoped to task; replaces the informal `(agent_id, task_id)` allowlist
3. **Provenance metadata** -- artifact writes carry `provenance` through the full pipeline; external-provenance content wrapped in delimiters before entering prompts
4. **Per-write audit for claude-code-agent** -- gateway intercept or post-commit diff audit
5. **Policy file for risk tiers** -- Tier 1/2 gates, configurable per project
