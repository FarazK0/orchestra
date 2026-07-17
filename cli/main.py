"""orchctl - Orchestra control CLI.

Commands
--------
create-task   Create a new task in the orchestrator
list          List tasks, optionally filtered by status
approve       Advance a task through the current human approval gate
run-task      Assemble the context package and start an agent run
review        Interactive approval loop: auto-validate and prompt for merge
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Optional

import httpx
import typer

app = typer.Typer(
    name="orchctl",
    help="Orchestra control CLI — human interface to the orchestrator.",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")


def _client() -> httpx.Client:
    return httpx.Client(base_url=_URL, timeout=10.0)


def _handle_error(resp: httpx.Response) -> None:
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Error {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# create-task
# ---------------------------------------------------------------------------


@app.command("create-task")
def create_task(
    title: str = typer.Argument(..., help="Short task title."),
    owner: str = typer.Option("human", help="Agent ID or 'human' that owns the task."),
    risk_tier: int = typer.Option(1, min=0, max=2, help="Risk tier: 0=auto, 1=batch, 2=blocking."),
    accept: Optional[list[str]] = typer.Option(
        None, "--accept", "-a", help="Acceptance criterion (repeatable)."
    ),
    input: Optional[list[str]] = typer.Option(
        None, "--input", "-i", help="Input artifact path (repeatable, relative to repo root)."
    ),
    output: Optional[list[str]] = typer.Option(
        None, "--output", "-o", help="Output artifact path (repeatable, relative to repo root)."
    ),
    depends_on: Optional[list[str]] = typer.Option(
        None, "--depends-on", "-d", help="Task ID this task depends on (repeatable)."
    ),
    tokens: int = typer.Option(100_000, help="Token budget."),
    wall_clock_min: int = typer.Option(30, help="Wall-clock budget in minutes."),
    retries: int = typer.Option(2, help="Retry budget."),
) -> None:
    """Create a new task and print its ID."""
    payload = {
        "title": title,
        "owner": owner,
        "risk_tier": risk_tier,
        "acceptance": accept or [],
        "inputs": input or [],
        "outputs": output or [],
        "depends_on": depends_on or [],
        "budget": {
            "tokens": tokens,
            "wall_clock_min": wall_clock_min,
            "retries": retries,
        },
    }
    with _client() as c:
        resp = c.post("/tasks", json=payload)
    _handle_error(resp)
    task = resp.json()
    typer.echo(f"Created {task['id']}: {task['title']!r}  [status: {task['status']}]")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

_STATUS_COLOURS = {
    "created": "",
    "assigned": "",
    "running": "",
    "completed": "",
    "validated": "",
    "merged": "",
    "closed": "",
    "failed": "",
    "escalated": "",
    "cancelled": "",
}


@app.command("list")
def list_tasks(
    status: Optional[list[str]] = typer.Option(
        None, "--status", "-s", help="Filter by status (repeatable)."
    ),
) -> None:
    """List tasks, newest last."""
    params: dict = {}
    if status:
        params["status"] = status

    with _client() as c:
        resp = c.get("/tasks", params=params)
    _handle_error(resp)

    tasks = resp.json()
    if not tasks:
        typer.echo("No tasks found.")
        return

    # Simple fixed-width table
    col_id = max(len("ID"), max(len(t["id"]) for t in tasks))
    col_st = max(len("STATUS"), max(len(t["status"]) for t in tasks))
    col_ow = max(len("OWNER"), max(len(t["owner"]) for t in tasks))
    fmt = f"{{:<{col_id}}}  {{:<{col_st}}}  {{:<{col_ow}}}  {{}}"

    typer.echo(fmt.format("ID", "STATUS", "OWNER", "TITLE"))
    typer.echo("-" * (col_id + col_st + col_ow + len("TITLE") + 6))
    for t in tasks:
        typer.echo(fmt.format(t["id"], t["status"], t["owner"], t["title"]))


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

# Statuses where human approval advances the task
_APPROVAL_GATES: dict[str, str] = {
    "created": "assigned",  # human kicks off execution
    "validated": "merged",  # human approves the reviewed branch
}


@app.command("approve")
def approve(
    task_id: str = typer.Argument(..., help="Task ID to approve, e.g. TASK-001."),
    actor: str = typer.Option("human", help="Actor name recorded in the audit log."),
) -> None:
    """Approve a task at the current human gate.

    \b
    created   → assigned  (kick off agent execution)
    validated → merged    (approve reviewed branch for merge)
    """
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}")
    _handle_error(resp)

    task = resp.json()
    current = task["status"]

    if current not in _APPROVAL_GATES:
        typer.echo(
            f"Error: {task_id} is in {current!r} — not at a human approval gate.\n"
            f"Gates are: {', '.join(f'{k!r}' for k in _APPROVAL_GATES)}",
            err=True,
        )
        raise typer.Exit(1)

    new_status = _APPROVAL_GATES[current]

    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": new_status, "actor": actor},
        )
    _handle_error(resp)

    typer.echo(f"Approved {task_id}: {current} → {new_status}")


# ---------------------------------------------------------------------------
# run-task
# ---------------------------------------------------------------------------


@app.command("run-task")
def run_task(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-001."),
    repo: str = typer.Option(..., "--repo", "-r", help="Absolute path to the managed Git repo."),
    agent_id: str = typer.Option("backend-agent", help="Agent ID recorded on the run."),
    store_dir: Optional[str] = typer.Option(
        None, help="Directory for context package files (default: {repo}/.orchestra/context)."
    ),
) -> None:
    """Assemble the context package and start an agent run.

    \b
    The task must already be in 'assigned' status (use 'approve' first).
    Creates a Run record in the control plane and writes the context package
    JSON to disk; the task transitions to 'running'.
    """
    payload: dict = {"agent_id": agent_id, "repo_path": repo}
    if store_dir:
        payload["store_dir"] = store_dir

    with _client() as c:
        resp = c.post(f"/tasks/{task_id}/run", json=payload)
    _handle_error(resp)

    run = resp.json()
    typer.echo(f"Run started for {task_id}")
    typer.echo(f"  run_id:  {run['run_id']}")
    typer.echo(f"  branch:  {run['branch']}")
    typer.echo(f"  context: {run['context_package_ref']}")


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


@app.command("merge")
def merge(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-001."),
    repo: str = typer.Option(..., "--repo", "-r", help="Absolute path to the managed Git repo."),
    actor: str = typer.Option("human", help="Actor name recorded in the audit log."),
    gateway_url: str = typer.Option(
        None,
        "--gateway-url",
        help="Gateway base URL. Defaults to $GATEWAY_URL or http://localhost:8081.",
    ),
) -> None:
    """Merge a validated task's agent branch into main and close the task.

    \b
    The task must be in 'validated' status.
    Steps performed (in order):
      1. git merge agent/<type>/{task_id} → main  (via gateway, audited)
      2. validated → merged                        (orchestrator transition)
      3. merged   → closed                         (orchestrator transition)
    """
    gw = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8081")

    # 1. Confirm task is validated before touching git.
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}")
    _handle_error(resp)
    task = resp.json()
    if task["status"] != "validated":
        typer.echo(
            f"Error: {task_id} is in {task['status']!r} — must be 'validated' to merge.",
            err=True,
        )
        raise typer.Exit(1)

    agent_type = task["owner"].removesuffix("-agent")
    branch = f"agent/{agent_type}/{task_id}"

    # 2. Git merge via gateway.
    with httpx.Client(base_url=gw, timeout=30.0) as gw_client:
        resp = gw_client.post(
            "/git/merge",
            json={"actor": actor, "task_id": task_id, "repo_path": repo, "branch": branch},
        )
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Gateway error {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)
    sha = resp.json().get("sha", "?")

    # 3. Transition validated → merged.
    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": "merged", "actor": actor, "payload": {"sha": sha}},
        )
    _handle_error(resp)

    # 4. Transition merged → closed.
    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": "closed", "actor": actor},
        )
    _handle_error(resp)

    typer.echo(f"Merged {task_id}: {branch} → main (sha: {sha}), task is now closed.")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@app.command("validate")
def validate(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-001."),
    repo: str = typer.Option(..., "--repo", "-r", help="Absolute path to the managed Git repo."),
    actor: str = typer.Option("validator", help="Actor name recorded in the audit log."),
) -> None:
    """Run the validator (ruff + pytest) on a completed task's agent branch.

    \b
    The task must be in 'completed' status.
    On pass:  task transitions to 'validated'.
    On fail:  task transitions to 'failed'.
    """
    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/validate",
            json={"repo_path": repo, "actor": actor},
        )
    _handle_error(resp)
    body = resp.json()
    task = body["task"]
    v = body.get("validation", {})
    typer.echo(f"Validated {task_id}: status is now {task['status']!r}")
    if v:
        ruff_rc = (v.get("ruff") or {}).get("returncode")
        pytest_rc = (v.get("pytest") or {}).get("returncode")
        typer.echo(f"  ruff   returncode={ruff_rc}")
        typer.echo(f"  pytest returncode={pytest_rc}")


# ---------------------------------------------------------------------------
# request — submit a change request to the root agent
# ---------------------------------------------------------------------------


@app.command("request")
def request_change(
    description: str = typer.Argument(..., help="Change request in plain language."),
    spec: Optional[str] = typer.Option(
        None,
        "--spec",
        "-s",
        help="Repo-relative path to a spec file for additional context.",
    ),
    redis_url: str = typer.Option(
        None, "--redis-url", help="Redis URL. Defaults to $REDIS_URL or redis://localhost:6380."
    ),
) -> None:
    """Submit a change request to the persistent root agent.

    \b
    The root agent (started by setup.sh) consumes this request, decomposes it
    into tasks using the planner, and dispatches sub-agents automatically.
    Monitor progress with: orchctl list
    """
    try:
        import redis as _redis

        from orchestrator.orchestrator.streams import ROOT_STREAM_KEY, StreamPublisher
    except ImportError as exc:
        typer.echo(f"Error: {exc}. Is the venv active?", err=True)
        raise typer.Exit(1)

    r_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6380")

    change_id = str(uuid.uuid4())
    publisher = StreamPublisher(r_url)
    try:
        publisher.publish(
            event_id=change_id,
            event_type="CHANGE_REQUEST",
            task_id=None,
            payload={"description": description, "spec_path": spec or ""},
            stream_key=ROOT_STREAM_KEY,
        )
    except _redis.exceptions.ConnectionError as exc:
        typer.echo(f"Error: could not connect to Redis at {r_url}: {exc}", err=True)
        typer.echo("Is the stack running? Try: make up", err=True)
        raise typer.Exit(1)
    finally:
        publisher.close()

    typer.echo(f"Change request submitted [{change_id[:8]}]:")
    typer.echo(f"  {description}")
    if spec:
        typer.echo(f"  spec: {spec}")
    typer.echo("")
    typer.echo("The root agent will decompose this into tasks and dispatch agents.")
    typer.echo("Monitor: uv run orchctl list")


# ---------------------------------------------------------------------------
# review — interactive approval loop
# ---------------------------------------------------------------------------

_G = "\033[32m"  # green
_R = "\033[31m"  # red
_B = "\033[1m"  # bold
_D = "\033[2m"  # dim
_X = "\033[0m"  # reset


def _g(s: str) -> str:
    return f"{_G}{s}{_X}"


def _r(s: str) -> str:
    return f"{_R}{s}{_X}"


def _b(s: str) -> str:
    return f"{_B}{s}{_X}"


def _d(s: str) -> str:
    return f"{_D}{s}{_X}"


def _show_validation(v: dict) -> None:
    ruff = v.get("ruff") or {}
    pytest_ = v.get("pytest") or {}

    ruff_ok = ruff.get("returncode") == 0
    pytest_rc = pytest_.get("returncode")
    pytest_ok = pytest_rc in (0, 5) if pytest_rc is not None else v.get("passed", False)

    typer.echo(f"  ruff   {_g('PASS') if ruff_ok else _r('FAIL')}")
    if not ruff_ok and ruff.get("stdout"):
        for line in ruff["stdout"].splitlines()[:8]:
            typer.echo(f"         {line}")

    typer.echo(f"  pytest {_g('PASS') if pytest_ok else _r('FAIL')}")
    if pytest_.get("stdout"):
        lines = pytest_["stdout"].splitlines()
        # Always show the summary line
        for line in reversed(lines):
            stripped = line.strip()
            if stripped and any(k in stripped for k in ("passed", "failed", "error", "no tests")):
                typer.echo(f"         {stripped}")
                break
        # On failure, show the short failure details too
        if not pytest_ok:
            in_fail = False
            for line in lines:
                if line.startswith("FAILED") or line.startswith("ERROR"):
                    in_fail = True
                if in_fail:
                    typer.echo(f"         {_r(line)}")
                if in_fail and not line.strip():
                    break


def _cancel_task(task_id: str, reason: str = "human cancelled") -> bool:
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": "cancelled", "actor": "human", "payload": {"reason": reason}},
        )
    if r.is_error:
        typer.echo(f"  {_r('Cancel error')}: {r.text}", err=True)
        return False
    typer.echo(f"  {_d('Cancelled')} {task_id}")
    return True


def _do_merge(task: dict, repo: str, gw: str) -> bool:
    task_id = task["id"]
    agent_type = task["owner"].removesuffix("-agent")
    branch = f"agent/{agent_type}/{task_id}"

    with httpx.Client(base_url=gw, timeout=30.0) as gw_client:
        resp = gw_client.post(
            "/git/merge",
            json={"actor": "human", "task_id": task_id, "repo_path": repo, "branch": branch},
        )
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        # Branch no longer exists (deleted after sandbox reset) — offer to cancel.
        branch_gone = "not something we can merge" in detail or "did not match" in detail
        typer.echo(f"  {_r('Merge error')}: {detail}", err=True)
        if branch_gone:
            typer.echo(f"  {_d('Branch')} {branch} {_d('no longer exists.')}")
            choice = (
                typer.prompt(f"  {_b('[c]')}ancel task   {_b('[s]')}kip", default="c")
                .strip()
                .lower()
            )
            if choice.startswith("c"):
                _cancel_task(task_id, reason="branch deleted")
        return False
    sha = resp.json().get("sha", "?")

    with _client() as c:
        r = c.post(f"/tasks/{task_id}/transition", json={"new_status": "merged", "actor": "human"})
    if r.is_error:
        typer.echo(f"  {_r('Transition error')}: {r.text}", err=True)
        return False

    with _client() as c:
        r = c.post(f"/tasks/{task_id}/transition", json={"new_status": "closed", "actor": "human"})
    if r.is_error:
        typer.echo(f"  {_r('Close error')}: {r.text}", err=True)
        return False

    typer.echo(f"  {_g('Merged')} {branch} → main  sha:{sha[:8]}  {_d('task closed')}")
    return True


def _handle_task(task: dict, repo: str, gw: str) -> None:
    task_id = task["id"]
    status = task["status"]

    typer.echo(f"\n  {'─' * 58}")
    typer.echo(f"  {_b(task_id)}  {_d(task['owner'])}")
    typer.echo(f"  {task['title']}")
    if task.get("acceptance"):
        for criterion in task["acceptance"]:
            typer.echo(f"  {_d('•')} {criterion}")

    if status == "completed":
        typer.echo(f"\n  Validating {task_id}...")
        with _client() as c:
            resp = c.post(
                f"/tasks/{task_id}/validate", json={"repo_path": repo, "actor": "validator"}
            )
        if resp.is_error:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            typer.echo(f"  {_r('Validation error')}: {detail}", err=True)
            return
        body = resp.json()
        v = body.get("validation", {})
        typer.echo("")
        _show_validation(v)
        if not v.get("passed", False):
            typer.echo(
                f"\n  {_r('Validation failed.')} The dispatcher will retry if budget allows."
            )
            return
        # Now it's validated; fall through to approval prompt below.
        task = body["task"]
        status = task["status"]

    if status == "validated":
        typer.echo(f"\n  Status: {_g('validated')}  Awaiting your approval.")
        choice = (
            typer.prompt(
                f"\n  {_b('[a]')}pprove + merge   {_b('[s]')}kip",
                default="a",
            )
            .strip()
            .lower()
        )
        if choice.startswith("a"):
            _do_merge(task, repo, gw)
        else:
            typer.echo("  Skipped — task stays validated.")

    elif status == "failed":
        typer.echo(f"\n  {_r('Failed.')} The dispatcher will retry or escalate.")

    elif status == "escalated":
        typer.echo(f"\n  {_r('Escalated.')} Retry budget exhausted.")
        choice = (
            typer.prompt(f"  {_b('[c]')}ancel task   {_b('[s]')}kip", default="s").strip().lower()
        )
        if choice.startswith("c"):
            _cancel_task(task_id, reason="escalated, human cancelled")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@app.command("cancel")
def cancel(
    task_id: str = typer.Argument(..., help="Task ID to cancel, e.g. TASK-007."),
    reason: str = typer.Option("human cancelled", help="Reason recorded in the audit log."),
) -> None:
    """Cancel a task from any non-terminal state.

    \b
    Valid from: created, assigned, running, completed, validated, failed, escalated.
    Use this to close stale tasks whose agent branches have been deleted.
    """
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}")
    _handle_error(resp)
    task = resp.json()

    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": "cancelled", "actor": "human", "payload": {"reason": reason}},
        )
    _handle_error(resp)
    typer.echo(f"Cancelled {task_id}: {task['status']} → cancelled  ({task['title']!r})")


@app.command("review")
def review(
    repo: str = typer.Option(..., "--repo", "-r", help="Managed repo path."),
    gateway_url: str = typer.Option(None, "--gateway-url", help="Gateway base URL."),
    poll: int = typer.Option(5, "--poll", help="Seconds between polls for completed tasks."),
) -> None:
    """Interactive approval loop: auto-validate completed tasks and prompt for merge.

    \b
    Polls the orchestrator for tasks in 'completed' or 'validated' state.
    For each completed task: runs validation (ruff + pytest) automatically.
    Then prompts for human approval (merge to main) or skip.
    Exits when all tasks are closed/failed/escalated, or on Ctrl+C.
    """
    gw = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8081")
    seen: set[str] = set()

    typer.echo(f"\n  {_b('Orchestra Review Loop')}  {_d('Ctrl+C to exit')}")
    typer.echo(f"  {_d('repo:')} {repo}  {_d('poll:')} {poll}s\n")

    _TERMINAL = {"closed", "failed", "escalated", "cancelled"}
    _ACTIVE = {"created", "assigned", "running", "completed", "validated"}
    _PENDING = {"completed", "validated"}

    try:
        while True:
            with _client() as c:
                resp = c.get("/tasks")
            _handle_error(resp)
            tasks = resp.json()

            if not tasks:
                typer.echo("  No tasks found.")
                break

            pending = [t for t in tasks if t["status"] in _PENDING]
            running = [t for t in tasks if t["status"] == "running"]
            done = all(t["status"] in _TERMINAL for t in tasks)

            # Present each pending task once (or re-present if it moved to validated).
            new_tasks = [t for t in pending if f"{t['id']}:{t['status']}" not in seen]

            if new_tasks:
                for task in new_tasks:
                    seen.add(f"{task['id']}:{task['status']}")
                    _handle_task(task, repo, gw)
            elif done:
                typer.echo(f"\n  {_g('All tasks finished.')} Review complete.")
                break
            else:
                n_running = len(running)
                n_done = sum(1 for t in tasks if t["status"] in _TERMINAL)
                typer.echo(
                    f"\r  {_d(f'running:{n_running}  done:{n_done}/{len(tasks)}  waiting...')}   ",
                    nl=False,
                )
                time.sleep(poll)

    except KeyboardInterrupt:
        typer.echo("\n\n  Exiting review loop.")


# ---------------------------------------------------------------------------
# Memory management
# ---------------------------------------------------------------------------

memory_app = typer.Typer(
    name="memory", help="Manage persistent agent memories.", no_args_is_help=True
)
app.add_typer(memory_app, name="memory")


@memory_app.command("list")
def memory_list(
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Filter by agent_id."),
    type_: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by memory_type."),
    project: str = typer.Option("default", "--project", help="Project ID."),
) -> None:
    """List agent memory entries."""
    params: dict = {"project_id": project}
    if agent:
        params["agent_id"] = agent
    if type_:
        params["memory_type"] = type_

    with _client() as c:
        resp = c.get("/agent-memories", params=params)
    _handle_error(resp)
    rows = resp.json()

    if not rows:
        typer.echo("  No memory entries found.")
        return

    typer.echo(
        f"\n  {'ID'[:8]:<10} {'AGENT':<22} {'TYPE':<12} {'KEY':<35} {'UPDATED':<20} LAST USED"
    )
    typer.echo(
        f"  {'-' * 8:<10} {'-' * 22:<22} {'-' * 10:<12} {'-' * 35:<35} {'-' * 19:<20} ---------"
    )
    for m in rows:
        last_used = (m.get("last_used_at") or "")[:19] or "(never)"
        typer.echo(
            f"  {m['id'][:8]:<10} {m['agent_id'][:22]:<22} {m['memory_type'][:12]:<12} "
            f"{m['key'][:35]:<35} {m['updated_at'][:19]:<20} {last_used}"
        )
    typer.echo(f"\n  {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")


@memory_app.command("show")
def memory_show(
    memory_id: str = typer.Argument(..., help="Full memory ID (UUID) or 8-char prefix."),
    agent: Optional[str] = typer.Option(
        None, "--agent", "-a", help="Agent ID (to resolve prefix)."
    ),
    project: str = typer.Option("default", "--project", help="Project ID."),
) -> None:
    """Show the full content of a memory entry."""
    params: dict = {"project_id": project}
    if agent:
        params["agent_id"] = agent

    with _client() as c:
        resp = c.get("/agent-memories", params=params)
    _handle_error(resp)
    rows = resp.json()

    match = next(
        (m for m in rows if m["id"] == memory_id or m["id"].startswith(memory_id)),
        None,
    )
    if match is None:
        typer.echo(f"  Memory {memory_id!r} not found.", err=True)
        raise typer.Exit(1)

    typer.echo(f"\n  ID:      {match['id']}")
    typer.echo(f"  Agent:   {match['agent_id']}")
    typer.echo(f"  Type:    {match['memory_type']}")
    typer.echo(f"  Key:     {match['key']}")
    typer.echo(f"  Updated: {match['updated_at']}")
    typer.echo(f"\n{match['content']}\n")


@memory_app.command("delete")
def memory_delete(
    memory_id: str = typer.Argument(..., help="Full memory ID (UUID) or 8-char prefix."),
    agent: Optional[str] = typer.Option(
        None, "--agent", "-a", help="Agent ID (to resolve prefix)."
    ),
    reason: str = typer.Option("human deleted", "--reason", "-r", help="Reason for deletion."),
    project: str = typer.Option("default", "--project", help="Project ID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a memory entry (writes an audit record before deletion)."""
    params: dict = {"project_id": project}
    if agent:
        params["agent_id"] = agent

    with _client() as c:
        resp = c.get("/agent-memories", params=params)
    _handle_error(resp)
    rows = resp.json()

    match = next(
        (m for m in rows if m["id"] == memory_id or m["id"].startswith(memory_id)),
        None,
    )
    if match is None:
        typer.echo(f"  Memory {memory_id!r} not found.", err=True)
        raise typer.Exit(1)

    if not yes:
        typer.echo(
            f"\n  Will delete: [{match['memory_type']}] {match['key']} ({match['agent_id']})"
        )
        typer.confirm("  Confirm deletion?", abort=True)

    with _client() as c:
        resp = c.request(
            "DELETE",
            f"/agent-memories/{match['id']}",
            json={"reason": reason},
        )
    _handle_error(resp)
    typer.echo(f"  Deleted memory {match['id'][:8]} ({match['key']})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
