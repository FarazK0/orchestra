# Orchestra

Human-centric multi-agent orchestration platform. Humans own intent, agents own
execution, the orchestrator owns governance.

Start here:
1. Read CLAUDE.md for invariants, layout, and current phase scope.
2. Read docs/design/orchestrator-mvp-v0.2.md for the full architecture and plan.
3. `cp .env.example .env`, then `make up && make migrate`.

Status: Phase 3 in progress. Phase 1 (walking skeleton) and Phase 2 (concurrency, DAG, multi-agent) complete. Phase 3 has shipped: persistent root agent (Step 23) and agent memory system (Step 24 + v2 improvements).
