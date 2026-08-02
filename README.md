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
- A CLI agent — `claude` (default), `gemini`, or any stdin-compatible LLM CLI

## Installation

Clone Orchestra once to a stable location — agents will manage *other* repos, not this one:

```bash
git clone https://github.com/your-org/orchestra ~/tools/orchestra
cd ~/tools/orchestra

# Install orchctl globally so it works from any directory:
make install       # runs: uv tool install --editable .
```

Copy the environment template and fill in your settings:

```bash
cp .env.example .env
# Edit .env — at minimum set:
#   DATABASE_URL=postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra
#   REDIS_URL=redis://localhost:6380
#   ANTHROPIC_API_KEY=sk-...  (only needed if using python-api backend)
```

Point Orchestra at the project you want agents to work on, then start services:

```bash
export SANDBOX_REPO_PATH=/path/to/your-project   # any git repo on your machine

make setup         # starts Postgres + Redis, runs migrations,
                   # starts orchestrator + gateway + dispatcher + root agent
```

Verify everything is healthy:

```bash
orchctl doctor     # pre-flight check: services, backends, tools, SANDBOX_REPO_PATH
```

You're ready:

```bash
orchctl request "add a login endpoint"
```

`orchctl` works from any directory once globally installed. `SANDBOX_REPO_PATH` tells it
which project to target; export it in your shell profile to make it permanent.

## Using it with any local repo

Orchestra manages a *target* repo — any git project on your machine. It never
touches the Orchestra repo itself. The only thing that ties it to a project is
`SANDBOX_REPO_PATH`.

### Point it at your project

```bash
export SANDBOX_REPO_PATH=/path/to/my-project
```

Add this to `~/.bashrc` (or `~/.zshrc`) if you're always working on the same project.
Switch projects any time by changing the variable — no restart needed.

### Submit a change request

```bash
orchctl request "add JWT auth to the login endpoint"
```

The root agent decomposes the request into tasks, creates branches in your repo
(`orchestra/<task-id>-<slug>`), dispatches agents, and tracks progress in the
control plane. Your repo's `main` branch is never touched until you explicitly approve.

### Watch progress

```bash
orchctl list                     # see all tasks and their status
orchctl show TASK-001            # full detail: inputs, outputs, validators, last run
```

### Review and merge

When tasks complete, run the interactive review loop:

```bash
orchctl review --repo $SANDBOX_REPO_PATH
```

For each completed task it:
1. Runs the assigned validators (ruff, pytest, mypy, etc.) on the agent's branch
2. Shows per-check results
3. Prompts you to merge or reject

Merge writes the branch into your repo's `main` and closes the task. Agents working
on dependent tasks are unblocked automatically.

You can also drive it step-by-step:

```bash
orchctl validate TASK-001 --repo $SANDBOX_REPO_PATH   # run validators manually
orchctl merge    TASK-001 --repo $SANDBOX_REPO_PATH   # merge after you're happy
```

### What agents do to your repo

- Create one branch per task: `orchestra/<task-id>-<slug>`
- Write only to the files listed in the task's `outputs` (enforced by a capability token)
- Commit to their branch; never push to `main` directly
- Append a summary entry to `WORKLOG.md` on merge

Nothing lands in `main` without your explicit `orchctl merge` (or `/orcui` approval).

### Switching projects

```bash
export SANDBOX_REPO_PATH=/path/to/other-project
orchctl doctor      # verify the new target is a git repo and services are healthy
orchctl request "..."
```

Tasks from the previous project remain in the control plane under their original path.
Each project gets its own task namespace derived from `SANDBOX_REPO_PATH`.

## Agent backends

Orchestra is agent-agnostic. Any CLI-based LLM can act as a worker, planner, or
validator. Backends are registered in `permissions/backends.yaml`:

```yaml
backends:
  claude-code:
    type: cli
    command: ["claude", "--dangerously-skip-permissions", "-p", "-"]
    input: stdin
  gemini:
    type: cli
    command: ["gemini", "-p", "-"]
    input: stdin
  python-api:
    type: python   # calls Anthropic API directly; requires ANTHROPIC_API_KEY
defaults:
  worker: claude-code
  planner: claude-code
  validator: claude-code
```

Configure per component — worker, planner, and validator can each use a different backend:

```bash
orchctl backend list                       # show available backends + availability
orchctl config set worker-backend gemini   # switch workers to gemini CLI
orchctl config set planner-backend python-api
orchctl config show                        # show all active settings
orchctl backend add aider --command "aider --yes-always -" --input file
```

Or via env vars: `WORKER_BACKEND`, `PLANNER_BACKEND`, `VALIDATOR_BACKEND`.

## Agent identities

`task.owner` is a **domain identity**, not an execution backend selector:

| Identity | Specialises in |
|---|---|
| `backend-agent` | APIs, data models, business logic, migrations |
| `frontend-agent` | HTML, CSS, JS, templates, browser interaction |
| `qa-agent` | Test plans, QA reports, risk assessment |
| `devops-agent` | Infrastructure, CI/CD, AWS, Terraform, GH Actions |
| *(any string)* | Custom specialisation — probes its own tools on first run; escalates to human if tools are missing |

Any owner string creates a new agent identity that accumulates expertise, episode memories,
and skill memories across tasks. The planner sees proven identities (via capability memories)
and routes future tasks to agents with confirmed tool access.

## Two UX paths

### Claude Code UI (recommended)

`make setup` launches a Claude Code session **inside the Orchestra directory** with
`/orcui` as your control panel. You never open Claude Code in your project — Orchestra
reaches into it via `SANDBOX_REPO_PATH`.

```
/orcui                          show platform status + task list
/orcui what should I do next?   get a recommended next action
/orcui request "add auth"       submit a change request to the root agent
/arch-to-tasks spec.md          decompose a spec file into a task plan
```

If you close the session and want to return to the UI later:

```bash
cd ~/tools/orchestra   # the Orchestra directory, not your project
claude /orcui
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

### Human-gate tasks

Tasks with `owner=human` block the DAG until a human fills a structured manifest:

```bash
orchctl human-done TASK-001 --repo PATH   # fill manifest + transition to validated
orchctl merge TASK-001 --repo PATH        # close and unblock successors
```

## Work history

Every merged task appends a timestamped entry to `WORKLOG.md` in the managed repo.
The entry includes the task title, branch, commit SHA, and a 2-3 sentence summary
written by the agent itself.

## Documentation

Full documentation is in [`docs/guide/`](docs/guide/):

| Guide | |
|-------|--|
| [Quickstart](docs/guide/quickstart.md) | Step-by-step first run |
| [Overview](docs/guide/overview.md) | Architecture and invariants |
| [Task Lifecycle](docs/guide/task-lifecycle.md) | State machine, all statuses |
| [Agents](docs/guide/agents.md) | Agent types, backends, and how to choose |
| [Validators](docs/guide/validators.md) | Pluggable quality checks |
| [Memory](docs/guide/memory.md) | Agent memory and expertise accumulation |
| [CLI Reference](docs/guide/cli-reference.md) | Every `orchctl` command |
| [Configuration](docs/guide/configuration.md) | Environment variables |
| [API Reference](docs/guide/api-reference.md) | HTTP API for both services |

`CLAUDE.md` at the repo root documents architecture invariants and contributor guidelines.
