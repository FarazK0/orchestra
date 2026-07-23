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

## Further reading

| Path | What's in it |
|------|-------------|
| `CLAUDE.md` | Architecture, invariants, every `orchctl` command documented |
| `docs/design/orchestrator-mvp-v0.2.md` | Full design doc — task model, DAG, gateway |
| `docs/design/phase1-retro.md` … `phase2-retro.md` | Phase retrospectives |
| `docs/adr/` | Architectural decision records (ADR-001 … N) |
| `permissions/validators.yaml` | Pluggable validator registry |
| `permissions/policy.yaml` | Tier policy (auto-merge vs human-gate) |
