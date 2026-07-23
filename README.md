# Orchestra

Human-centric multi-agent orchestration. You own intent, agents own execution,
the orchestrator owns governance.

```
You → Orchestrator → Dispatcher → Agents → Gateway → your-project/
```

Every side effect is audited through the gateway. Nothing merges to your repo
without human approval.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — Python package manager
- Docker — Postgres + Redis
- git
- [claude CLI](https://www.npmjs.com/package/@anthropic-ai/claude-code) *(recommended)* — `npm install -g @anthropic-ai/claude-code`

## Quick start

```bash
git clone <repo> && cd orchestra

# Point Orchestra at the project you want agents to work on:
export SANDBOX_REPO_PATH=/path/to/your-project

make setup        # installs deps, starts Postgres + Redis, runs migrations,
                  # starts orchestrator + gateway + dispatcher + root agent
                  # then hands off to Claude Code UI (or terminal, your choice)

./orchctl quickstart   # print cheat-sheet (works offline, before make setup)
```

`./orchctl` is a zero-config wrapper — works right after `make setup` or `uv sync`.
For a persistent global install so `orchctl` works from any directory: `make install`.

## Two UX paths

### Claude Code UI (recommended)

`make setup` asks whether you want the Claude Code path. If yes, it opens a Claude
Code session in this directory with `/orcui` as your control panel.

```
/orcui                          show platform status + task list
/orcui what should I do next?   get a recommended next action
/orcui request "add auth"       submit a change request to the root agent
/arch-to-tasks spec.md          decompose a spec file into a task plan
```

No CLI commands to memorise — describe what you want in plain English.

### Terminal (direct)

```bash
orchctl request "add auth"              submit a change request
orchctl list                            show all tasks
orchctl show TASK-001                   full detail + validation history
orchctl validate TASK-001 --repo PATH   run assigned validators on agent branch
orchctl review --repo PATH              interactive validate-and-approve loop
orchctl merge TASK-001 --repo PATH      merge validated branch to main
orchctl COMMAND --help                  command-specific help
```

## Documentation

Full documentation is in [`docs/guide/`](docs/guide/):

| Guide | |
|-------|--|
| [Quickstart](docs/guide/quickstart.md) | Step-by-step first run |
| [Overview](docs/guide/overview.md) | Architecture and invariants |
| [Task Lifecycle](docs/guide/task-lifecycle.md) | State machine, all statuses |
| [Agents](docs/guide/agents.md) | Agent types and how to choose |
| [Validators](docs/guide/validators.md) | Pluggable quality checks |
| [Memory](docs/guide/memory.md) | Agent memory and expertise accumulation |
| [CLI Reference](docs/guide/cli-reference.md) | Every `orchctl` command |
| [Configuration](docs/guide/configuration.md) | Environment variables |
| [API Reference](docs/guide/api-reference.md) | HTTP API for both services |

`CLAUDE.md` at the repo root documents architecture invariants and contributor guidelines.
