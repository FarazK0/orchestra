# orcui

Orchestra natural-language UI — a conversational control panel for the Orchestra platform.
Describe what you want in plain English; this skill maps it to the right command, runs it,
presents the result clearly, and tells you what to do next.

## Usage

```
/orcui
/orcui <natural language request>
```

Examples:
- `/orcui` — show current platform state
- `/orcui show failed tasks`
- `/orcui approve TASK-005`
- `/orcui what should I do next?`
- `/orcui start new project with spec auction-store-architecture.md`

## What this skill does

1. **Always first:** runs a platform state snapshot and presents it as a compact status panel.
2. **If arguments were given:** interprets them as a natural-language request and executes immediately after the snapshot.
3. **If no arguments:** prints an action menu of things the user can ask.
4. **After every action:** prints the exact command used and a "Next steps" section.

## Instructions

### Step 1 — Platform state snapshot

Run all of these and present the combined result:

```bash
curl -sf http://localhost:8080/healthz && echo "orchestrator: UP" || echo "orchestrator: DOWN"
curl -sf http://localhost:8081/healthz && echo "gateway: UP" || echo "gateway: DOWN"
```

```bash
cd /mnt/d/orc/orchestra && uv run python -m cli.main list
```

```bash
git -C /mnt/d/orc/orchestra/sandbox/sample-project branch --show-current 2>/dev/null || echo "(no sandbox repo)"
```

Present as:

```
━━ Orchestra Status ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Orchestrator  http://localhost:8080   UP / DOWN
  Gateway       http://localhost:8081   UP / DOWN
  Sandbox       <current branch>

━━ Tasks ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ID        STATUS      OWNER              TITLE
  ...

━━ Ask me anything ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  (shown only when no args were given — see action menu below)
```

### Step 2 — Interpret the request (if args given)

Map natural language to commands using this table. Run the command, print it, print the result.

| User says | Run |
|---|---|
| show tasks / list | `uv run python -m cli.main list` |
| show \<status\> tasks | `uv run python -m cli.main list --status <status>` |
| approve task N / approve TASK-00N | `uv run python -m cli.main approve TASK-00N` |
| run task N | `uv run python -m cli.main run-task TASK-00N --repo sandbox/sample-project` |
| validate task N | `uv run python -m cli.main validate TASK-00N --repo sandbox/sample-project` |
| merge task N | `uv run python -m cli.main merge TASK-00N --repo sandbox/sample-project` |
| review / approval loop | `uv run python -m cli.main review --repo sandbox/sample-project` |
| request / submit change "\<desc\>" | `uv run python -m cli.main request "<desc>"` |
| start project / new run \<spec\> | `bash scripts/setup.sh --spec <spec>` |
| show logs / tail logs | `tail -50 /tmp/orchestra/logs/dispatcher.log /tmp/orchestra/logs/root-agent.log` |
| show memory / agent memory | `uv run python -m cli.main memory list` |
| show memory for \<agent\> | `uv run python -m cli.main memory list --agent <agent>` |
| tail task N / show logs for task N | `uv run python -m cli.main tail TASK-00N` |
| audit task N / what has task N done | `uv run python -m cli.main audit TASK-00N` |
| what should I do next? | analyse task states (see suggestions logic below) and recommend |
| stop services | show PIDs from `/tmp/orchestra/pids/` and the `make stop` command |
| wipe / reset / clean db | **show commands only, do not run** (see destructive ops below) |

**Command prefix:** all CLI commands run from `/mnt/d/orc/orchestra`. Always `cd` there first or use absolute paths.

**Task ID normalisation:** if the user says "task 5" or "task-5", normalise to "TASK-005".

### Step 3 — No args: action menu

If no arguments were given, after the state snapshot print:

```
What would you like to do?

  Task flow
    /orcui show <status> tasks         — filter by status (running / failed / escalated)
    /orcui approve TASK-00N            — advance through approval gate
    /orcui validate TASK-00N           — run ruff + pytest on agent branch
    /orcui merge TASK-00N              — merge validated branch into main
    /orcui review                      — interactive approval loop for all ready tasks

  Starting work
    /orcui request "add feature X"     — submit change request to root agent
    /orcui start project <spec-file>   — full setup + task plan from a spec

  Observability
    /orcui show logs                   — tail dispatcher + root-agent logs
    /orcui show memory                 — list all agent memory entries
    /orcui what should I do next?      — get a recommended next action

  If something went wrong
    /orcui cancel TASK-00N             — cancel a task (show command, confirm first)
    /orcui wipe database               — safe steps to reset Postgres + Redis
```

### Output format rules

- **Before every result:** print `> <exact command run>` in a code block.
- **Task lists:** markdown table — `ID | STATUS | OWNER | TITLE`. Bold status for `running` and `assigned`; plain for `closed`/`cancelled`.
- **Errors:** if a command fails, show the error, explain what it likely means, and suggest a fix.
- **After every action:** always end with a **Next steps** section (2–3 items).

### Suggestions logic

| Current state | Suggested next steps |
|---|---|
| Task is `assigned` | Approve it → run it |
| Task is `running` | Watch logs; validate when done |
| Task is `completed` | Validate it |
| Task is `validated` | Merge it |
| Task is `failed` | Check logs; cancel or retry |
| Task is `escalated` | Check logs; investigate root cause; cancel and re-request |
| All tasks `closed` | Start next change request or new project |
| Services down | Run `bash scripts/setup.sh` to start everything |

### Destructive operations — show, don't run

For any of these, print the commands in a code block and say "Run these yourself — they cannot be undone":

- **Wipe database:**
  ```bash
  make down
  sudo rm -rf ~/.orchestra/pgdata
  make up
  make migrate
  ```

- **Cancel a task:**
  ```bash
  uv run python -m cli.main cancel TASK-00N
  ```
  Ask the user to confirm before running cancel.

- **Delete memory:**
  ```bash
  uv run python -m cli.main memory delete <MEMORY-ID> --yes
  ```

### Tool notes

- Use the Bash tool to run all commands.
- Run all CLI commands from `/mnt/d/orc/orchestra` (not the sandbox).
- Do not call external APIs or write files (except tasks.json via arch-to-tasks).
- If a service is down, do not attempt to start it automatically — show the user the command.
- Keep responses concise: one status panel, one result block, one next-steps section.
