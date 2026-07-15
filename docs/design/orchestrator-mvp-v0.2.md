# Human-Centric Multi-Agent Orchestration Platform
## Revised Architecture and Action Plan (v0.2)

Status: Design revision of v0.1
Scope: Explanation of the current architecture, critique of the original proposal, concrete design improvements, and a build-ready action plan.

---

## Part 1: The Current Architecture (v0.1) Explained

Before the critique, this section explains what v0.1 actually is, how its pieces fit together, and why each exists. This is the mental model everything else builds on.

### 1.1 The core inversion

Most multi-agent frameworks (AutoGen, CrewAI, early AutoGPT-style systems) are conversation-first: agents talk to each other in a loop, and the plan emerges from the chat. v0.1 inverts this. The plan is authored by a human, execution is decomposed into a task graph, and agents are workers attached to nodes of that graph. Conversation is the exception, not the medium. The one-line philosophy, "humans own intent, agents own execution, the orchestrator owns governance," is a separation of powers: no single component both decides what to do and does it.

The mental model to hold is a software engineering organization, not a swarm. There is a product owner (the human), a project management layer (the orchestrator), individual contributors with job descriptions (agents), a shared codebase (the repository), a ticketing system (tasks), status announcements (events), and code review before anything lands (validator, reviewer, human approval).

### 1.2 The six components and what each one does

**The Human Planner** is the only source of intent in the system. The human creates projects, defines objectives and priorities, approves plans and merges, and resolves conflicts agents cannot settle themselves. Critically, the human is a component of the architecture, not a user outside it: approval gates are wired into the task lifecycle, so the system structurally cannot complete certain transitions without human input.

**The Orchestrator Engine** is deliberately unintelligent about the problem domain. It never writes code, never designs architecture, never answers questions. It is pure governance: it holds the task DAG, decides which tasks are unblocked and dispatches them, issues and enforces permissions, routes events between agents, detects conflicts, records everything to the audit log, and applies retry and escalation policies. Keeping the orchestrator dumb is a deliberate choice: if the coordinator itself reasoned about the problem, it would become another autonomous decision-maker, which is exactly what the design is trying to avoid.

**Agents** are narrow specialists. Each has exactly one responsibility (research, backend, frontend, QA, documentation) and is defined by a bundle of identity, prompt, skills, tools, memory, permissions, event subscriptions, and an output validator. The key framing in v0.1 is that agents own responsibilities, not projects: a backend agent has no opinion about the roadmap, it implements the API tasks it is assigned. Specialization keeps prompts focused, keeps permission scopes small, and makes behavior predictable.

**The Shared Project Repository** is the collaboration medium. Instead of agents holding context in a conversation, everything important is externalized as an artifact in a structured directory tree: requirements, architecture, roadmap, task records, ADRs, generated artifacts (API specs, diagrams, reports), code, events, permissions, and audit records. Every object carries metadata (owner, status, dependencies, outputs), and the repository is the single source of truth. If it is not in the repository, it did not happen.

**The Event Bus** is how agents learn about each other's progress without talking. When the backend agent finishes an API, it emits `API_CREATED` with a payload; the frontend, QA, and documentation agents are subscribed to that event type and react independently. This is classic publish-subscribe: producers do not know or care who consumes, subscribers only receive event types they registered for, and events are persisted so they can be inspected and replayed. Direct agent-to-agent conversation is permitted only in three narrow cases (missing information, needed clarification, active conflict), and even then the exchange is stored so future agents can inspect it.

**The Validator/Reviewer** is the quality gate between agent output and shared state. It checks formatting, policy compliance, tests, schemas, and security before work reaches human review. It exists so the human approval gate reviews substance, not lint errors.

### 1.3 The security model: three layers

v0.1 layers three complementary permission ideas.

First, **graph-based RBAC**: an agent's role, and therefore its baseline permissions, derive from its position in the execution graph. The architect node can touch architecture and approvals; the backend node can touch backend code and API contracts; the database node sits under backend and can touch only schema and migrations. Siblings cannot modify each other's artifacts unless explicitly granted. Permissions mirror the org chart.

Second, **capability tokens**: rather than holding permanent role permissions, an agent receives a temporary, task-scoped capability when assigned work. The capability for "Implement Login API" grants read on the auth spec, write on the auth service, execute on tests, and explicitly nothing else: no repository deletion, no merging to main, no touching architecture. Capabilities expire when the task ends. This is the principle of least privilege applied per-task rather than per-role, so a misbehaving agent's blast radius is bounded by its current assignment.

Third, **branch isolation**: every agent works in its own workspace branch, and nothing modifies `main` directly. All changes flow through the merge pipeline: agent commit → validator → reviewer → human → merge. This is Git-flow discipline applied to AI output.

### 1.4 State and memory

v0.1 distinguishes four memory scopes, each with a different lifetime. Working memory is the agent's context for the current task, discarded on completion. Project memory is the shared repository, persistent for the life of the project. Agent memory holds agent-specific preferences (the backend agent prefers FastAPI and SQLAlchemy) and persists across tasks. Decision memory (ADRs) is permanent: architectural decisions and their rationale are never deleted, so future agents and humans can understand why the system is shaped the way it is.

### 1.5 Lifecycle, failure, and conflict

Tasks move through an explicit state machine: created → assigned → running → completed → validated → merged → closed. The failure arm is equally explicit: running → failed → retry, and if retries are exhausted, escalate to human, then cancel. Nothing loops silently.

Conflicts are treated as first-class events. The canonical scenario: backend changes an API that frontend depends on. The system detects the conflict, emits a conflict event, gives the affected agent a chance to resolve it automatically, and escalates to human review if resolution fails. Combined with the audit system, which records timestamp, agent, action, artifact, reason, and result for every action, the design goal is that any state the system reaches can be explained after the fact.

### 1.6 Why this shape

Each unusual choice in v0.1 traces back to one of three goals. **Predictability:** shared state and DAG execution make runs reproducible in a way that free-form agent chat never is. **Safety:** scoped permissions, capability expiry, branch isolation, and human gates bound what any single agent can do. **Auditability:** events, ADRs, stored conversations, and the audit log mean the system's history is inspectable, which is a prerequisite for trusting it with real work. The success criteria at the end of v0.1 are essentially these three goals restated as testable claims.

---

## Part 2: Assessment of v0.1

The core thesis is sound and genuinely differentiating: humans own intent, agents own execution, the orchestrator owns governance, and collaboration happens through shared state and events rather than open-ended agent chat. That is the right instinct. Most multi-agent frameworks fail because they are conversation-first, which makes them non-deterministic, expensive, and impossible to audit.

However, v0.1 has five structural problems that will hurt during implementation:

**1. The MVP is too big.** Phase 1 already implies a custom repository format, audit system, task schema, and agent runtime. Graph-based RBAC, capability tokens, event replay, and branch workspaces are all listed before anything ships. The design describes the v1.0 destination, not an MVP. The single most important revision is a brutal cut of Phase 1 scope.

**2. The "Shared Project Repository" reinvents Git.** The proposal describes branches, merges, commits, reviews, and immutable history, then proposes building a custom repository structure on top of Postgres and object storage. Git already provides content-addressed storage, branching, merge conflict detection, signed commits (attribution), and diff-based review. Use a real Git repo as the artifact store from day one and reserve Postgres for orchestrator state (tasks, events, capabilities, audit index). This one decision deletes months of work.

**3. Concurrency is unspecified.** Two agents writing to overlapping artifacts, a task graph where a dependency is invalidated mid-flight, an event consumed twice after a crash: none of these have defined semantics. Without idempotency keys, optimistic locking, and at-least-once delivery with deduplication, the system will corrupt state under exactly the concurrent workloads it is designed for.

**4. Capability tokens have no mechanism.** "Temporary scoped capabilities" is the right idea, but v0.1 never says how a capability is issued, carried, verified, or revoked. Without a concrete mechanism, permissions collapse into orchestrator-side if-statements, which are fine for an MVP but should be named as such.

**5. Security model ignores the actual threat.** The dominant risk in this system is not an agent exceeding its RBAC role. It is prompt injection through artifacts: a research agent ingests a hostile web page, writes a poisoned summary into `artifacts/`, and a downstream backend agent reads it as trusted context. Shared state is a shared attack surface. v0.1 treats all repository content as trusted; it must not be.

Smaller gaps worth fixing: no context assembly strategy (agents cannot be handed the whole repo; token budgets force a deliberate "context packing" step), no cost accounting per task, no schema registry for events, no story for testing the orchestrator itself, and human approval as designed will become the bottleneck (every merge gated on a human does not scale past a handful of tasks per day).

---

## Part 3: Design Improvements

### 3.1 Git as the artifact plane, Postgres as the control plane

Two planes with a clean boundary:

- **Artifact plane (Git):** requirements, architecture docs, ADRs, code, reports, diagrams. Agents work on branches (`agent/backend/TASK-104`), commit with structured messages referencing the task ID, and merge via pull-request-like review. Attribution is free via committer identity; audit of content changes is free via `git log`.
- **Control plane (Postgres):** task DAG, task lifecycle state, agent registry, capability records, event log, cost ledger, audit index. This is the orchestrator's database and agents never touch it directly; they interact only through the orchestrator API.

Events live in the control plane as an append-only table (event sourcing). This gives replay and deterministic reconstruction of orchestrator state for free, which is also how you test the orchestrator: replay a recorded event log and assert the resulting state.

### 3.2 Redis Streams, not NATS or RabbitMQ, for the MVP

You already run Redis. Redis Streams gives consumer groups, at-least-once delivery, pending-entry tracking for crash recovery, and persistence. NATS/RabbitMQ are justified later if you need cross-datacenter fan-out or very high throughput; for an MVP they are pure operational overhead. Pair Streams with an idempotency key on every event (`event_id` = UUID, consumers record processed IDs) so duplicate delivery is harmless.

### 3.3 Capability mechanism: signed short-lived tokens checked at the tool boundary

Concrete design:

- When the orchestrator assigns a task, it mints a capability token: a signed JWT (or PASETO) containing `task_id`, `agent_id`, allowed operations as explicit tuples (`read: paths`, `write: paths`, `execute: commands`, `emit: event_types`), an expiry (task deadline plus margin), and a nonce for revocation lookup.
- Agents never hold raw credentials (no Git push keys, no API keys). Every side effect goes through the orchestrator's tool gateway, which verifies the token, checks the operation against the embedded scopes, checks the revocation list, executes on the agent's behalf, and writes the audit record atomically with the action.
- This makes enforcement structural rather than advisory: an agent that hallucinates a forbidden action simply cannot perform it, because the gateway is the only path to side effects.

For Phase 1, implement this as a plain orchestrator-side permission check keyed on `(agent_id, task_id)`; keep the token format for Phase 3. The important part is the invariant, established from day one: **agents have no direct access to anything; all effects flow through the gateway.**

### 3.4 Trust levels on artifacts (prompt injection defense)

Add a `provenance` field to every artifact's metadata:

- `human`: written or approved by a human. Trusted.
- `agent`: produced by an agent from trusted inputs. Semi-trusted.
- `external`: contains or derives from web content, third-party docs, or user uploads. Untrusted.

Rules: untrusted content is always wrapped in delimiter tags when injected into a prompt, is never placed in the system prompt, and instructions found inside it are ignored by convention baked into every agent prompt. A validator flags any artifact whose provenance would be laundered upward (external content copied into an `agent`-provenance file without marking). This is cheap to implement and addresses the most likely real-world failure.

### 3.5 Risk-tiered approvals instead of human-gates-everything

Classify every merge by risk:

- **Tier 0 (auto-merge):** docs, reports, test additions that pass the validator. Merged automatically, human notified.
- **Tier 1 (batch review):** ordinary code changes. Queued; human reviews in batches at their convenience; the DAG continues on other branches meanwhile.
- **Tier 2 (blocking approval):** architecture changes, schema migrations, permission changes, anything touching `main` config, releases. Hard gate.

The human stays in control of direction without becoming a synchronous bottleneck on every commit. Tier assignment rules live in a policy file (`permissions/policy.yaml`) that only humans can edit (Tier 2 by definition).

### 3.6 Context assembly as an explicit orchestrator function

Do not let agents "read the repo." For each task, the orchestrator assembles a context package: the task spec, acceptance criteria, the artifacts listed in the task's `inputs`, relevant ADRs, and the interface contracts of dependencies (e.g., the OpenAPI spec, not the backend source). This bounds token cost, makes runs reproducible (the exact context package is stored with the run record), and doubles as the read-permission mechanism: the context package IS the read scope.

### 3.7 Task schema with acceptance criteria and budgets

Extend the task object:

```json
{
  "id": "TASK-104",
  "title": "Implement Authentication",
  "owner": "backend-agent",
  "status": "in_progress",
  "depends_on": ["TASK-100"],
  "inputs": ["artifacts/api.yaml", "decisions/ADR-003.md"],
  "outputs": ["code/auth/"],
  "acceptance": [
    "POST /login returns token, expires_at, refresh_token",
    "All new endpoints covered by tests",
    "Validator passes: lint, tests, schema"
  ],
  "risk_tier": 1,
  "budget": {"tokens": 200000, "wall_clock_min": 30, "retries": 2},
  "run_history": []
}
```

Acceptance criteria are what the validator and the reviewer check against; without them, "Completed" is meaningless. Budgets prevent runaway loops and make cost per task a first-class metric.

### 3.8 Failure semantics

Define these explicitly:

- Every task run is idempotent from the orchestrator's view: a run either produces a committed branch state plus a completion event, or nothing (branch reset).
- Retries get a fresh branch from the same base; the failed branch is preserved for forensics.
- After `budget.retries` failures, the task enters `escalated` and emits `HUMAN_ATTENTION_NEEDED` with the failure summary. It never silently loops.
- Events use at-least-once delivery plus consumer-side dedup on `event_id`.
- Direct agent-to-agent questions become `QUESTION` / `ANSWER` events routed through the bus, so they inherit persistence, audit, and permission checks automatically. There is no separate "direct channel" to secure.

### 3.9 Observability from day one

You already know this stack well, which makes it cheap: OpenTelemetry traces per task run (span per LLM call, per tool call, per validator check), Prometheus metrics (tasks by state, queue depth, tokens per task, cost per task, validator pass rate, human queue latency), Grafana dashboard. The trace of a task run is also the debugging story for "why did the agent do that."

### 3.10 Model access through one abstraction

Route all LLM calls through a single client layer (LiteLLM or a thin homemade wrapper) that handles provider selection, retries with backoff, token counting, and cost recording into the ledger. This is also where cost-aware model routing plugs in later (cheap model for docs tasks, strong model for architecture), which connects directly to your OpenEnvelope routing ideas: the "cheapest combo that clears the quality bar" policy is exactly the router this platform will eventually want.

---

## Part 4: Revised Phasing

The rule: every phase ends with a demo that a skeptical engineer would accept as working.

**Phase 1 (Walking skeleton):** One human, one agent, real Git, real audit. Human creates a project and tasks via CLI (no UI yet). Orchestrator assigns a task to a single backend agent, assembles the context package, agent produces a branch, validator runs lint plus tests, human merges via normal Git review. Postgres holds tasks and the event log. Permissions are a hardcoded allowlist. Demo: "I typed a task, an agent shipped a reviewed, merged, tested change, and I can show the full audit trail."

**Phase 2 (Concurrency):** Two to three agents (backend, frontend or docs, QA), Redis Streams event bus, DAG scheduling with dependency gating, idempotent event consumption, conflict detection on overlapping outputs, risk-tiered auto-merge for Tier 0. Demo: two agents working concurrently on dependent tasks, an API_CREATED event triggering downstream work, no state corruption after killing the orchestrator mid-run and restarting.

**Phase 3 (Governance):** Signed capability tokens verified at the tool gateway, provenance tracking with the injection-defense rules, policy file for tier assignment, escalation flow, batch review UI (first real UI, and it should be the review queue, not the project creator, because that is where the human actually spends time). Demo: an agent attempting an out-of-scope write is refused by the gateway and the refusal is in the audit log.

**Phase 4 (Scale and polish):** Web UI for project and DAG visualization, event replay tooling, metrics dashboard, cost-aware model routing, dynamic agent spawning with inherited-and-narrowed capabilities. Demo: a multi-day project with a dozen tasks completed with under N minutes of human attention per task.

Deferred beyond MVP entirely: multi-project orchestration, reputation scoring, agent marketplace, cross-project knowledge graphs. All good ideas, all v2.

---

## Part 5: Detailed Action Plan

Assumes roughly 10 to 15 focused hours per week alongside other commitments. Adjust the calendar, keep the ordering.

### Phase 1: Walking skeleton (Weeks 1 to 4)

**Week 1: Foundations**
1. Repo setup: monorepo with `orchestrator/`, `agents/`, `gateway/`, `schemas/`, `infra/`. Python 3.12, FastAPI, uv or poetry, ruff, pytest, pre-commit.
2. Define JSON Schemas for Task, Event, AgentIdentity, RunRecord in `schemas/` with generated Pydantic models. Schemas are versioned from day one (`schema_version` field).
3. Postgres via Docker Compose; tables: `tasks`, `events` (append-only), `runs`, `audit`. Alembic migrations.
4. Decision to record as ADR-001 through ADR-004: Git-as-artifact-plane, Postgres-as-control-plane, gateway-mediated side effects, event sourcing for orchestrator state.

**Week 2: Orchestrator core**
5. Task CRUD API plus CLI (`orchctl create-task`, `orchctl list`, `orchctl approve`).
6. Task state machine (created → assigned → running → completed → validated → merged → closed, with failed/escalated arms) implemented as explicit transitions with guards; every transition writes an event and an audit row in one transaction.
7. Context packager: given a task, produce the context package (task spec, input artifacts read from Git, acceptance criteria) and persist it with the run record.

**Week 3: Agent runtime and gateway**
8. Tool gateway service: endpoints for `read_artifact`, `write_artifact`, `run_command` (sandboxed in a Docker container with no network by default), `emit_event`. Permission check is a simple allowlist keyed on `(agent_id, task_id)`. Every call audited.
9. Single backend agent: system prompt template, LLM client wrapper with token/cost logging, loop of plan → act via gateway → self-check against acceptance criteria → commit to branch `agent/backend/{task_id}`.
10. Structured commit messages: `[TASK-104] message` so Git history joins cleanly to the control plane.

**Week 4: Validation and the first demo**
11. Validator: ruff plus pytest plus schema checks run against the branch in a clean container; result written as an event and attached to the run record.
12. Merge flow: human reviews the branch diff (plain `git diff` or GitHub PR if hosted), `orchctl merge TASK-104` performs the merge through the gateway and closes the task.
13. End-to-end demo task: "add a /health endpoint with a test to this sample FastAPI project." Record the demo; write a short retro of what hurt.

Exit criteria: one task flows end to end; killing any component mid-run leaves recoverable state; the audit table plus git log reconstruct exactly what happened.

### Phase 2: Concurrency (Weeks 5 to 8)

**Week 5: Event bus**
14. Redis Streams integration: one stream per project, consumer groups per agent, `event_id` dedup table, pending-entry reclaim on restart. Contract test: publish 100 events, kill a consumer at random points, assert exactly-once effects.
15. Migrate the Phase 1 direct calls (orchestrator → agent) to event-driven task assignment.

**Week 6: DAG scheduling**
16. DAG engine: topological readiness computation, only unblocked tasks dispatched, dependency invalidation (upstream reopened → downstream flagged stale).
17. Concurrency guard: tasks with overlapping `outputs` paths cannot run simultaneously; detected at scheduling time.

**Week 7: Multi-agent**
18. Add frontend (or docs) agent and QA agent with their own prompts and gateway scopes. QA subscribes to `TASK_VALIDATED` events and files structured issue reports as artifacts.
19. QUESTION/ANSWER event flow between agents, persisted, with a hop limit (a question chain longer than 2 hops escalates to human, preventing agent chat loops).

**Week 8: Resilience and Tier 0**
20. Retry policy with fresh-branch semantics and escalation events; poison-task handling.
21. Tier 0 auto-merge for validator-passing docs/test-only changes; notification to human.
22. Demo: three-task dependent chain (backend API → frontend consumption → QA report) with concurrent execution and one injected failure recovering via retry.

### Phase 3: Governance (Weeks 9 to 12)

**Week 9:** Capability tokens (PASETO or JWT with ES256), minting on assignment, gateway verification, revocation list in Redis, expiry tied to task budget.
**Week 10:** Provenance metadata on artifacts, prompt-injection defenses in context packaging (delimiter wrapping, provenance rules in every agent system prompt), validator check for provenance laundering. Adversarial test: plant an instruction in a research artifact and verify the downstream agent does not follow it and the gateway blocks the attempted effect.
**Week 11:** Policy file for risk tiers, Tier 2 hard gates, batch review queue as the first web UI (Next.js or plain HTMX; the review queue matters more than aesthetics).
**Week 12:** Observability pass: OTel traces per run, Prometheus metrics, Grafana dashboard (tasks by state, cost per task, validator pass rate, human queue latency). Demo: the refused out-of-scope write, shown in the audit log and the trace.

### Phase 4: Scale and polish (Weeks 13 to 16)

**Week 13:** Project/DAG visualization UI; event replay CLI (`orchctl replay --until <event_id>`) used to build a regression suite for the orchestrator itself.
**Week 14:** Cost-aware model routing in the LLM client layer (per-task-class model selection with quality gates).
**Week 15:** Dynamic spawning: an agent may request a sub-agent; orchestrator mints a capability strictly narrower than the parent's; spawn depth limited by policy.
**Week 16:** Hardening week: load test the bus, chaos-kill components during a 20-task project, fix what breaks, write the v1.0 doc from what was actually built.

---

## Part 6: Risks and open questions

**Biggest technical risk:** validator quality. If the validator is weak, Tier 0 auto-merge is unsafe and every merge falls back to the human, which kills the value proposition. Mitigation: invest in acceptance-criteria-driven checks early and track validator false-pass rate as a first-class metric.

**Biggest product risk:** the human review queue is the real UX. If reviewing agent output is slower than doing the work, the platform loses. Design the review experience (good diffs, run summaries, acceptance-criteria checklists) as carefully as the orchestration.

**Open questions to resolve during Phase 1:**
- Hosted Git (GitHub, using PRs and Actions as the review/validator substrate) versus self-managed bare repos through the gateway. Hosted is faster to build; self-managed keeps the capability model pure. Recommendation: start hosted, keep the gateway abstraction so it is swappable.
- Whether agent memory (preferences like "backend agent prefers FastAPI") is worth having at all in the MVP, or whether it belongs in the agent prompt as configuration. Recommendation: configuration, not memory, until Phase 4.
- Sandbox depth for `run_command`: Docker-no-network is Phase 1; decide in Phase 3 whether gVisor/Firecracker-level isolation is warranted for untrusted-provenance code.
