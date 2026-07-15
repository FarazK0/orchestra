"""orchctl - Orchestra control CLI.

Commands
--------
create-task   Create a new task in the orchestrator
list          List tasks, optionally filtered by status
approve       Advance a task through the current human approval gate
run-task      Assemble the context package and start an agent run
"""

from __future__ import annotations

import os
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
      1. git merge agent/backend/{task_id} → main  (via gateway, audited)
      2. validated → merged                         (orchestrator transition)
      3. merged   → closed                          (orchestrator transition)
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

    branch = f"agent/backend/{task_id}"

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
    task = resp.json()
    typer.echo(f"Validated {task_id}: status is now {task['status']!r}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
