# Quickstart

Get Orchestra running against your project in about 10 minutes.

## Prerequisites

| Tool | Install | Required for |
|------|---------|-------------|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | Python package management |
| Docker | [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) | Postgres + Redis |
| git | system package manager | Repo operations |
| [claude CLI](https://www.npmjs.com/package/@anthropic-ai/claude-code) | `npm install -g @anthropic-ai/claude-code` | Recommended UI + claude-code-agent |

The `claude` CLI is optional but strongly recommended — it powers both the `/orcui`
control panel and the `claude-code-agent` worker (which needs no API key of its own).

---

## Step 1 — Clone

```bash
git clone https://github.com/FarazK0/orchestra.git
cd orchestra
```

---

## Step 2 — Run setup

```bash
make setup
```

The setup script walks you through several prompts:

### 2a. Choose your target project

Orchestra manages a Git repo of your choice — not its own codebase.

```
Which project should Orchestra manage?

  1  Enter a path  — use an existing local repo
  2  Use the built-in demo  — sandbox/sample-project (good for trying it out)
```

Enter the path to an existing repo you want agents to work on, or choose option 2
to use the bundled sample project for a quick demo.

You can also skip the prompt by exporting `SANDBOX_REPO_PATH` before running setup:

```bash
export SANDBOX_REPO_PATH=/path/to/your-project
make setup
```

The chosen path is written to `.env` as `SANDBOX_REPO_PATH` so all services pick it up.

### 2b. Choose your interface

```
  1  Claude Code UI  (recommended)
  2  Direct / terminal mode
```

Option 1 opens a Claude Code session after setup completes, with `/orcui` ready to use
as your control panel. Requires the `claude` CLI.

### 2c. Choose your agent type

```
  1  Claude Code agent  (recommended)
  2  Custom Python agents  (backend / frontend / QA loops)
```

- **Claude Code agent** — the `claude` CLI is the agent worker. No `ANTHROPIC_API_KEY`
  needed. Agents run as `claude-code-agent`.
- **Python agents** — custom loops that call the Anthropic API directly. Requires
  `ANTHROPIC_API_KEY` in `.env`. Tasks can be routed to `backend-agent`,
  `frontend-agent`, or `qa-agent` separately.

### What setup does

1. Checks prerequisites (Python 3.12+, Docker, git)
2. `uv sync` — installs Python packages into `.venv/`
3. Installs `orchctl` globally via `uv tool install`
4. Creates `.env` from `.env.example` and generates `CAPABILITY_SECRET`
5. Writes your chosen `SANDBOX_REPO_PATH` to `.env`
6. `docker compose up -d` — starts Postgres (port 5433) and Redis (port 6380)
7. `alembic upgrade head` — applies all database migrations
8. Initialises the target repo with `git init` if it doesn't already have one
9. Starts orchestrator (8080), gateway (8081), dispatcher, and root agent as background processes
10. Waits for both services to pass health checks
11. Hands off to the Claude Code UI or the review loop depending on your choice

---

## Step 3 — Use it

### Claude Code path (recommended)

If you chose Claude Code UI in step 2b, a session opens automatically. Use `/orcui`:

```
/orcui                              show platform status + all tasks
/orcui what should I do next?       get a recommended next action
/orcui request "add user auth"      submit a change request to the root agent
/arch-to-tasks spec.md              decompose a spec file into a task plan
```

### Terminal path

```bash
# Submit a change request — the root agent decomposes it into tasks and
# dispatches agents automatically.
orchctl request "add a login page with email and password"

# Watch the task list
orchctl list

# When an agent finishes, validate its work
orchctl validate TASK-001 --repo /path/to/your-project

# Review and merge (interactive loop)
orchctl review --repo /path/to/your-project
```

---

## Platform lifecycle

```bash
make setup        # start everything (idempotent — safe to re-run)
make stop         # stop all background services
make logs         # tail all logs (orchestrator, gateway, dispatcher, root-agent)
make migrate      # apply new DB migrations (after pulling updates)
make test         # run the test suite
make lint         # ruff check + format
```

Service logs are at `/tmp/orchestra/logs/` and PIDs at `/tmp/orchestra/pids/`.

---

## Re-running setup

`make setup` is idempotent. Running it again:
- Skips `uv sync` if the venv is current
- Skips `.env` creation if the file exists
- Regenerates `CAPABILITY_SECRET` only if it is still the placeholder value
- Skips `docker compose up` if containers are already running
- Skips `git init` if the target repo already has `.git/`
- Skips service startup if the PID files show they are already running

---

## Troubleshooting

**Services fail to start**: check `tail -f /tmp/orchestra/logs/*.log`.

**`orchctl: command not found`**: either `make install` failed silently (run
`uv tool install --editable .` manually) or uv's tool bin path is not on `PATH`.
Use `./orchctl` from the repo root as a fallback.

**`401 Unauthorized` from the gateway**: the `CAPABILITY_SECRET` in `.env` does not
match the one the orchestrator used to mint the token. Run `make stop && make setup`
to regenerate a consistent secret and restart.

**Postgres connection refused**: `docker compose ps` to check the container is up.
Port 5432 may be taken on your machine — Orchestra maps to 5433 on the host by design.

**Migration errors**: `make clean-db` tears down the Postgres volume and re-migrates
from scratch. This deletes all task data.
