# Orchestra Documentation

## Guides

| Guide | What it covers |
|-------|---------------|
| [Overview](./overview.md) | Architecture, components, invariants, security model |
| [Quickstart](./quickstart.md) | Get running in 10 minutes |
| [Task Lifecycle](./task-lifecycle.md) | State machine, all statuses, human gates, failure recovery |
| [Agents](./agents.md) | Agent types, how they run, context package, tools, custom agents |
| [Validators](./validators.md) | Pluggable registry, per-validator reference, adding custom checks |
| [Memory](./memory.md) | Identity, episode, and skill memory; domain expertise accumulation |
| [CLI Reference](./cli-reference.md) | Every `orchctl` command with options and examples |
| [Configuration](./configuration.md) | Environment variables, permission files, Docker compose |
| [API Reference](./api-reference.md) | Orchestrator (8080) and Gateway (8081) HTTP API |

## Design documents

Internal design docs and architectural decisions are in the parent directory:

| Document | What it covers |
|----------|---------------|
| [`../design/orchestrator-mvp-v0.2.md`](../design/orchestrator-mvp-v0.2.md) | Full architecture specification |
| [`../design/phase1-retro.md`](../design/phase1-retro.md) | Phase 1 retrospective |
| [`../design/phase2-retro.md`](../design/phase2-retro.md) | Phase 2 retrospective |
| [`../adr/`](../adr/) | Architectural Decision Records (ADR-001 … N) |

## Quick orientation

**New to Orchestra?** → [Quickstart](./quickstart.md)

**Understand what's happening to a task?** → [Task Lifecycle](./task-lifecycle.md)

**Choose an agent type?** → [Agents](./agents.md)

**Add a validator or understand how checks work?** → [Validators](./validators.md)

**Look up a CLI command?** → [CLI Reference](./cli-reference.md)

**Configure the platform for a new environment?** → [Configuration](./configuration.md)

**Build an integration or add a custom agent?** → [API Reference](./api-reference.md)
