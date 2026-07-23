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
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
import typer
from dotenv import load_dotenv

load_dotenv()

try:
    from agents.shared.llm import LLMClient as _LLMClient

    _HAS_LLM = True
except Exception:
    _HAS_LLM = False
    _LLMClient = None  # type: ignore[assignment]

app = typer.Typer(
    name="orchctl",
    help="Orchestra control CLI — human interface to the orchestrator.",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")


def _client(timeout: float = 10.0) -> httpx.Client:
    return httpx.Client(base_url=_URL, timeout=timeout)


def _handle_error(resp: httpx.Response) -> None:
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Error {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Validator helpers (used by create-task and the validator subcommand)
# ---------------------------------------------------------------------------


def _fetch_registry() -> list[dict]:
    """Return the validator registry from the orchestrator, or [] on failure."""
    try:
        with _client() as c:
            resp = c.get("/validators")
        if resp.is_error:
            return []
        return resp.json().get("validators", [])
    except Exception:
        return []


def _select_validators(outputs: list[str]) -> list[str]:
    """Interactively select validators for a task based on its outputs.

    Fetches the registry, auto-detects candidates, presents them to the user,
    and lets the user accept, edit (add/remove), or skip the selection.
    Returns the final list of validator names to store on the task.
    """
    registry = _fetch_registry()
    if not registry:
        return []

    # Separate always-on built-ins (shown for info, not stored in task.validators)
    always_on = [v for v in registry if v.get("always_run")]
    auto_detectable = [v for v in registry if not v.get("always_run") and v.get("auto_detect", True)]

    # Auto-detect from output paths
    suggested: list[str] = []
    for v in auto_detectable:
        exts = v.get("match_extensions", [])
        paths_kw = v.get("match_paths", [])
        for out in outputs:
            out_lower = out.lower()
            if any(out_lower.endswith(e) for e in exts) or any(p in out_lower for p in paths_kw):
                suggested.append(v["name"])
                break

    # Display what will run
    typer.echo("\n  Validators for this task:\n")
    for v in always_on:
        typer.echo(f"    {_d('~')} {v['name']:<18} {_d(v.get('description',''))}  {_d('(always-on)')}")
    if suggested:
        for name in suggested:
            info = next((v for v in registry if v["name"] == name), {})
            typer.echo(f"    {_g('✓')} {name:<18} {info.get('description','')}")
    else:
        typer.echo(f"    {_d('(no validators auto-detected from outputs)')}")

    opt_in = [v for v in auto_detectable if v["name"] not in suggested]
    if opt_in:
        typer.echo(f"\n  {_d('Available (not auto-detected):')}")
        for v in opt_in:
            typer.echo(f"    {_d('-')} {v['name']:<18} {_d(v.get('description',''))}")

    typer.echo("")
    choice = typer.prompt(
        "  Accept validators? [Y/n/edit]",
        default="Y",
    ).strip().lower()

    if choice in ("", "y", "yes"):
        return suggested

    if choice in ("n", "no"):
        typer.echo("  No validators assigned — only always-on checks will run.")
        return []

    # Edit mode: +name to add, -name to remove
    selected = list(suggested)
    all_names = {v["name"] for v in registry if not v.get("always_run")}
    typer.echo(
        f"\n  Edit mode — type +<name> to add, -<name> to remove, 'done' to finish."
        f"\n  Current: {', '.join(selected) or '(none)'}"
        f"\n  Available: {', '.join(sorted(all_names))}"
    )
    while True:
        cmd = typer.prompt("  >").strip()
        if cmd.lower() in ("done", "ok", ""):
            break
        if cmd.startswith("+"):
            name = cmd[1:].strip()
            if name in all_names and name not in selected:
                selected.append(name)
                typer.echo(f"  Added {name}. Current: {', '.join(selected)}")
            elif name not in all_names:
                typer.echo(f"  Unknown validator {name!r}. Available: {', '.join(sorted(all_names))}")
            else:
                typer.echo(f"  {name} already in list.")
        elif cmd.startswith("-"):
            name = cmd[1:].strip()
            if name in selected:
                selected.remove(name)
                typer.echo(f"  Removed {name}. Current: {', '.join(selected) or '(none)'}")
            else:
                typer.echo(f"  {name} not in current list.")
        else:
            typer.echo("  Use +<name> or -<name>.")

    return selected


# ---------------------------------------------------------------------------
# validator — subcommand group
# ---------------------------------------------------------------------------

_validator_app = typer.Typer(help="Manage and list validators.")
app.add_typer(_validator_app, name="validator")


@_validator_app.command("list")
def validator_list() -> None:
    """List available validators from the registry."""
    registry = _fetch_registry()
    if not registry:
        typer.echo("No validators found (is the orchestrator running?)")
        return

    name_w = max(len(v["name"]) for v in registry)
    typer.echo(f"\n  {'NAME':<{name_w}}  {'AUTO':<5}  DESCRIPTION")
    typer.echo(f"  {'-' * name_w}  -----  -----------")
    for v in registry:
        always = v.get("always_run", False)
        auto = "yes" if (always or v.get("auto_detect", True)) else "opt"
        badge = " (always-on)" if always else ""
        typer.echo(f"  {v['name']:<{name_w}}  {auto:<5}  {v.get('description','')}{badge}")
    typer.echo("")


# ---------------------------------------------------------------------------
# create-task
# ---------------------------------------------------------------------------


@app.command("create-task")
def create_task(
    title: str = typer.Argument(..., help="Short task title."),
    owner: str = typer.Option("human", help="Agent ID or 'human' that owns the task."),
    risk_tier: Optional[int] = typer.Option(
        None,
        min=0,
        max=2,
        help="Risk tier: 0=auto, 1=batch, 2=blocking. Default: auto-assigned from policy.",
    ),
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
    outputs_list = output or []
    chosen_validators = _select_validators(outputs_list)

    payload = {
        "title": title,
        "owner": owner,
        "risk_tier": risk_tier,
        "acceptance": accept or [],
        "inputs": input or [],
        "outputs": outputs_list,
        "depends_on": depends_on or [],
        "validators": chosen_validators,
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
    "suspended": "",
    "blocked": "",
    "awaiting_human": "",
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
        tier_badge = " [T2]" if t.get("risk_tier") == 2 else ""
        typer.echo(fmt.format(t["id"], t["status"], t["owner"], t["title"] + tier_badge))


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
    tier_2_override: bool = typer.Option(
        False, "--tier-2-override", help="Required to merge a Tier 2 (blocking) task."
    ),
) -> None:
    """Approve a task at the current human gate.

    \b
    created   → assigned  (kick off agent execution)
    validated → merged    (approve reviewed branch for merge)

    Tier 2 tasks require --tier-2-override to proceed to 'merged'.
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

    # Tier 2 guard: require explicit flag before sending the transition request.
    if new_status == "merged" and task.get("risk_tier") == 2 and not tier_2_override:
        typer.echo(
            f"Error: {task_id} ({task['title']!r}) is Tier 2 (blocking approval).\n"
            "Review the diff carefully, then re-run with --tier-2-override to confirm.",
            err=True,
        )
        raise typer.Exit(1)

    details: dict = {}
    if new_status == "merged" and tier_2_override:
        details["tier2_override"] = True

    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": new_status, "actor": actor, "details": details},
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
    tier_2_override: bool = typer.Option(
        False, "--tier-2-override", help="Required to merge a Tier 2 (blocking) task."
    ),
) -> None:
    """Merge a validated task's agent branch into main and close the task.

    \b
    The task must be in 'validated' status.
    Steps performed (in order):
      1. git merge agent/<type>/{task_id} → main  (via gateway, audited)
      2. validated → merged                        (orchestrator transition)
      3. merged   → closed                         (orchestrator transition)

    Tier 2 tasks require --tier-2-override to proceed.
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

    # Tier 2 guard.
    if task.get("risk_tier") == 2 and not tier_2_override:
        typer.echo(
            f"Error: {task_id} ({task['title']!r}) is Tier 2 (blocking approval).\n"
            "Review the diff carefully, then re-run with --tier-2-override to confirm.",
            err=True,
        )
        raise typer.Exit(1)

    # Derive branch from the most recent run (handles retries like -retry-2).
    try:
        with _client() as c:
            runs_resp = c.get(f"/tasks/{task_id}/runs")
        runs_resp.raise_for_status()
        runs = runs_resp.json()
        branch = runs[0]["branch"] if runs else None
    except Exception:
        branch = None
    if not branch:
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
    merge_details: dict = {}
    if tier_2_override:
        merge_details["tier2_override"] = True
    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={
                "new_status": "merged",
                "actor": actor,
                "payload": {"sha": sha},
                "details": merge_details,
            },
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
    with _client(timeout=120.0) as c:
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
        _show_validation(v)


# ---------------------------------------------------------------------------
# show — full task detail + validation summary
# ---------------------------------------------------------------------------


@app.command("show")
def show_task(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-001."),
) -> None:
    """Show full detail for a task, including its most recent validation result."""
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}")
    _handle_error(resp)
    task = resp.json()

    typer.echo(f"\n  {_b(task['id'])}  {_d(task['owner'])}  [{task['status']}]")
    typer.echo(f"  {task['title']}\n")

    if task.get("inputs"):
        typer.echo(f"  {_d('Inputs:')}   {', '.join(task['inputs'])}")
    if task.get("outputs"):
        typer.echo(f"  {_d('Outputs:')}  {', '.join(task['outputs'])}")
    if task.get("validators"):
        typer.echo(f"  {_d('Validators:')} {', '.join(task['validators'])}")
    if task.get("acceptance"):
        typer.echo(f"  {_d('Acceptance criteria:')}")
        for criterion in task["acceptance"]:
            typer.echo(f"    {_d('•')} {criterion}")

    risk_tier = task.get("risk_tier", 1)
    if risk_tier == 2:
        typer.echo(f"\n  {_r('[TIER 2 — BLOCKING APPROVAL]')}")

    # Fetch validation result
    with _client() as c:
        v_resp = c.get(f"/tasks/{task_id}/validation")
    if v_resp.is_error:
        typer.echo(f"\n  {_d('(validation data unavailable)')}")
    else:
        vdata = v_resp.json()
        if vdata.get("validation") is None:
            typer.echo(f"\n  {_d('No validation recorded yet.')}")
        else:
            validated_at = vdata.get("validated_at", "")
            if validated_at:
                typer.echo(f"\n  {_d('Validated at:')} {validated_at[:19].replace('T', ' ')} UTC")
            _show_validation(vdata["validation"])
    typer.echo("")


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
    checks: list[dict] = v.get("checks", [])
    summary = v.get("summary", "")
    passed = v.get("passed", False)

    if not checks:
        # Legacy format fallback (pre-registry validator)
        ruff = v.get("ruff") or {}
        pytest_ = v.get("pytest") or {}
        ruff_ok = ruff.get("returncode") == 0
        pytest_rc = pytest_.get("returncode")
        pytest_ok = pytest_rc in (0, 5) if pytest_rc is not None else passed
        typer.echo(f"  ruff   {_g('PASS') if ruff_ok else _r('FAIL')}")
        typer.echo(f"  pytest {_g('PASS') if pytest_ok else _r('FAIL')}")
        return

    verdict = _g("PASSED") if passed else _r("FAILED")
    typer.echo(f"\n  Validation: {verdict}  ({summary})\n")

    # Per-check table
    name_w = max((len(c["name"]) for c in checks), default=10)
    out_w = 52
    for chk in checks:
        icon = _g("✓") if chk["passed"] else _r("✗")
        name_col = chk["name"].ljust(name_w)
        first_line = (chk.get("output") or "").splitlines()[0][:out_w]
        dur = f"({chk.get('duration_s', 0):.1f}s)"
        typer.echo(f"    {icon} {name_col}  {first_line:<{out_w}}  {_d(dur)}")

    # Failure details: show full output for failed checks
    failed_checks = [c for c in checks if not c["passed"]]
    if failed_checks:
        typer.echo("")
        for chk in failed_checks:
            typer.echo(f"  {_r('✗')} {chk['name']} output:")
            for line in (chk.get("output") or "").splitlines()[:20]:
                typer.echo(f"    {line}")


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


def _do_merge(task: dict, repo: str, gw: str, tier2_override: bool = False) -> bool:
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

    merge_details: dict = {}
    if tier2_override:
        merge_details["tier2_override"] = True
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": "merged", "actor": "human", "details": merge_details},
        )
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
        is_tier2 = task.get("risk_tier") == 2
        if is_tier2:
            typer.echo(
                f"\n  Status: {_g('validated')}  {_r('[TIER 2 — BLOCKING APPROVAL]')}\n"
                f"  This task touches high-risk files. Review the diff before approving."
            )
            confirm_id = typer.prompt(
                "\n  Type the task ID to approve, or press Enter to skip",
                default="",
            ).strip()
            if confirm_id == task["id"]:
                _do_merge(task, repo, gw, tier2_override=True)
            else:
                typer.echo("  Skipped — task stays validated.")
        else:
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

    elif status == "awaiting_human":
        cp = task.get("checkpoint") or {}
        typer.echo(f"\n  {_r('Awaiting your input')} ({cp.get('question_type', '?')}):")
        typer.echo(f"  {_b('Q:')} {cp.get('question', '(no question recorded)')}")
        choices = cp.get("choices") or []
        for i, choice_text in enumerate(choices):
            typer.echo(f"    {i + 1}. {choice_text}")
        if cp.get("context"):
            typer.echo(f"  Context: {_d(cp['context'])}")
        if cp.get("current_progress"):
            typer.echo(f"  Progress so far: {_d(cp['current_progress'])}")
        response = typer.prompt("\n  Your answer (Enter to skip)").strip()
        if response:
            with _client() as c:
                r = c.post(
                    f"/tasks/{task_id}/respond",
                    json={"response": response, "actor": "human"},
                )
            if not r.is_error:
                typer.echo(f"  {_g('Response recorded.')} Dispatcher will re-launch the agent.")
            else:
                try:
                    detail = r.json().get("detail", r.text)
                except Exception:
                    detail = r.text
                typer.echo(f"  {_r('Error:')} {detail}", err=True)
        else:
            typer.echo("  Skipped — task stays awaiting_human.")

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


@app.command("recover")
def recover(
    task_id: str = typer.Argument(..., help="Task ID to recover, e.g. TASK-007."),
    note: str = typer.Option("", "--note", "-n", help="Reason for manual recovery."),
) -> None:
    """Mark an escalated task as completed so it can be validated and merged.

    \b
    Use when an agent's work is done but the task is stuck in 'escalated' after
    repeated run failures. After recovery, run 'validate' then 'merge'.
    """
    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": "completed", "actor": "human", "details": {"recovery_note": note}},
        )
    _handle_error(resp)
    typer.echo(f"{task_id}: escalated -> completed. Run 'validate' next.")


@app.command("resume")
def resume(
    task_id: str = typer.Argument(..., help="Task ID to resume, e.g. TASK-007."),
    note: str = typer.Option("", "--note", "-n", help="Optional note recorded in the audit log."),
) -> None:
    """Resume a suspended task from its last committed checkpoint.

    \b
    The agent will receive the list of commits already made and will continue
    from where it left off rather than starting from scratch.
    The task must be in 'suspended' status.
    """
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}")
    _handle_error(resp)
    task = resp.json()
    if task["status"] != "suspended":
        typer.echo(f"Error: {task_id} is {task['status']!r}, not 'suspended'.", err=True)
        raise typer.Exit(1)

    with _client() as c:
        resp = c.post(
            f"/tasks/{task_id}/transition",
            json={"new_status": "assigned", "actor": "human", "details": {"resume_note": note}},
        )
    _handle_error(resp)
    typer.echo(f"{task_id}: suspended -> assigned. Dispatcher will re-launch the agent.")


@app.command("questions")
def questions() -> None:
    """List tasks waiting for human input.

    \b
    Shows each task's question, choices (if any), and context.
    Use 'orchctl respond TASK-ID "answer"' to reply.
    """
    with _client() as c:
        resp = c.get("/tasks", params={"status": ["awaiting_human"]})
    _handle_error(resp)
    tasks = resp.json()
    if not tasks:
        typer.echo("No tasks awaiting human input.")
        return
    for t in tasks:
        cp = t.get("checkpoint") or {}
        typer.echo(f"\n  {_b(t['id'])}  {_d(t['owner'])}")
        typer.echo(f"  {t['title']}")
        typer.echo(f"  Type: {cp.get('question_type', '?')}")
        typer.echo(f"  {_b('Q:')} {cp.get('question', '(no question recorded)')}")
        choices = cp.get("choices") or []
        for i, choice_text in enumerate(choices):
            typer.echo(f"    {i + 1}. {choice_text}")
        if cp.get("context"):
            typer.echo(f"  Context: {_d(cp['context'])}")
        if cp.get("current_progress"):
            typer.echo(f"  Progress: {_d(cp['current_progress'])}")
    n = len(tasks)
    typer.echo(f'\n  {n} task(s) awaiting input.  Run: orchctl respond TASK-ID "answer"')


@app.command("respond")
def respond(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-007."),
    response: str = typer.Argument(..., help="Your answer to the agent's question."),
    actor: str = typer.Option("human", help="Actor name recorded in the audit log."),
) -> None:
    """Answer a question from an agent waiting for human input.

    \b
    The agent will be restarted with your response injected into its context
    and continue from where it left off.
    Use 'orchctl questions' to see what tasks are waiting.
    """
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}")
    _handle_error(resp)
    task = resp.json()
    if task["status"] != "awaiting_human":
        typer.echo(f"Error: {task_id} is {task['status']!r}, not 'awaiting_human'.", err=True)
        raise typer.Exit(1)
    cp = task.get("checkpoint") or {}
    typer.echo(f"\n  Question: {cp.get('question', '?')}")
    typer.echo(f"  Your answer: {response}\n")
    with _client() as c:
        resp = c.post(f"/tasks/{task_id}/respond", json={"response": response, "actor": actor})
    _handle_error(resp)
    typer.echo(f"{task_id}: response recorded. Dispatcher will re-launch the agent.")


@app.command("review")
def review(
    repo: str = typer.Option(..., "--repo", "-r", help="Managed repo path."),
    gateway_url: str = typer.Option(None, "--gateway-url", help="Gateway base URL."),
    poll: int = typer.Option(5, "--poll", help="Seconds between polls for completed tasks."),
) -> None:
    """Interactive approval loop: auto-validate completed tasks and prompt for merge.

    \b
    Polls the orchestrator for tasks in 'completed' or 'validated' state.
    For each completed task: runs all assigned validators automatically.
    Then prompts for human approval (merge to main) or skip.
    Exits when all tasks are closed/failed/escalated, or on Ctrl+C.
    """
    gw = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8081")
    seen: set[str] = set()

    typer.echo(f"\n  {_b('Orchestra Review Loop')}  {_d('Ctrl+C to exit')}")
    typer.echo(f"  {_d('repo:')} {repo}  {_d('poll:')} {poll}s\n")

    _TERMINAL = {"closed", "failed", "escalated", "cancelled"}
    _ACTIVE = {"created", "assigned", "running", "completed", "validated"}
    _PENDING = {"completed", "validated", "awaiting_human"}

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
            # awaiting_human tasks are re-presented if the human skipped them last time
            # (seen key changes only on status change, not on repeated skip).
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
# tail — stream agent log for a running (or finished) task
# ---------------------------------------------------------------------------


@app.command("tail")
def tail(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-006."),
    lines: int = typer.Option(100, "--lines", "-n", help="Lines to show when task is finished."),
) -> None:
    """Stream the live agent log for a task.

    \b
    If the task is still running: follows the log in real time (Ctrl+C to stop).
    If the task has finished: prints the last --lines lines and exits.
    """
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}/runs")
    _handle_error(resp)
    runs = resp.json()

    if not runs:
        typer.echo(f"No runs found for {task_id}.", err=True)
        raise typer.Exit(1)

    latest = runs[0]
    log_path = latest.get("log_path")
    if not log_path:
        typer.echo(f"No log file recorded for {task_id} run {latest['run_id'][:8]}.", err=True)
        raise typer.Exit(1)

    p = Path(log_path)
    if not p.exists():
        typer.echo(f"Log file not found: {log_path}", err=True)
        raise typer.Exit(1)

    is_running = latest.get("finished_at") is None
    typer.echo(
        f"  {_b(task_id)}  run:{latest['run_id'][:8]}  "
        f"{'[running — Ctrl+C to stop]' if is_running else '[finished]'}"
    )
    typer.echo(f"  {_d(log_path)}\n")

    if is_running:
        # Follow mode: stream new bytes as they arrive.
        try:
            with p.open("r") as f:
                # Print what's already there.
                sys.stdout.write(f.read())
                sys.stdout.flush()
                # Then follow new output.
                while True:
                    chunk = f.read(4096)
                    if chunk:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    else:
                        # Re-check if run finished.
                        with _client() as c:
                            r = c.get(f"/tasks/{task_id}/runs")
                        if r.is_success and r.json() and r.json()[0].get("finished_at"):
                            # Drain remaining output.
                            sys.stdout.write(f.read())
                            sys.stdout.flush()
                            break
                        time.sleep(1)
        except KeyboardInterrupt:
            typer.echo("\n")
    else:
        # Static mode: print last N lines.
        content = p.read_text(encoding="utf-8", errors="replace")
        all_lines = content.splitlines()
        for line in all_lines[-lines:]:
            typer.echo(line)


# ---------------------------------------------------------------------------
# audit — show gateway audit trail for a task
# ---------------------------------------------------------------------------


@app.command("audit")
def audit(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-006."),
) -> None:
    """Show the gateway audit trail for a task (most recent first)."""
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}/audit")
    _handle_error(resp)
    rows = resp.json()

    if not rows:
        typer.echo(f"No audit rows found for {task_id}.")
        return

    typer.echo(f"\n  {_b(task_id)} — {len(rows)} audit row(s)\n")
    typer.echo(f"  {'TIMESTAMP':<26} {'ACTION':<30} DETAILS")
    typer.echo(f"  {'-' * 25:<26} {'-' * 29:<30} {'─' * 40}")
    for row in rows:
        ts = row["timestamp"][:19].replace("T", " ")
        action = row["action"][:30]
        details = row.get("details") or {}
        failure_reason = details.get("failure_reason", "")
        details_str = str(details)
        if len(details_str) > 400:
            details_str = details_str[:397] + "..."
        typer.echo(f"  {ts:<26} {action:<30} {_d(details_str)}")
        if failure_reason and "failed" in action or "escalated" in action:
            typer.echo(f"  {'':26}   {_r('failure_reason:')} {failure_reason}")


# ---------------------------------------------------------------------------
# why — quick diagnostic panel for failed/escalated tasks
# ---------------------------------------------------------------------------


@app.command("why")
def why(
    task_id: str = typer.Argument(..., help="Task ID, e.g. TASK-001."),
    repo: str = typer.Option(
        None, "--repo", "-r", envvar="SANDBOX_REPO_PATH", help="Managed repo path."
    ),
    lines: int = typer.Option(25, "--lines", "-n", help="Run log lines to show."),
) -> None:
    """Show a diagnostic panel explaining why a task failed or escalated."""
    import subprocess as _sp

    with _client() as c:
        task_resp = c.get(f"/tasks/{task_id}")
        _handle_error(task_resp)
        task = task_resp.json()

        events_resp = c.get(f"/tasks/{task_id}/events")
        _handle_error(events_resp)
        events = events_resp.json()

        runs_resp = c.get(f"/tasks/{task_id}/runs")
        log_path: str | None = None
        if runs_resp.status_code == 200:
            runs = runs_resp.json()
            if runs:
                log_path = runs[0].get("log_path")

    status = task.get("status", "?")
    budget = task.get("budget") or {}
    retry_count = task.get("retry_count", 0)
    max_retries = budget.get("retries", 2)

    # Extract failure reason from last TASK_FAILED or TASK_ESCALATED event
    failure_reason = "unknown"
    for ev in events:
        etype = ev.get("event_type", "")
        if etype in ("TASK_FAILED", "TASK_ESCALATED"):
            payload = ev.get("payload") or {}
            fr = payload.get("failure_reason") or payload.get("last_failure_reason")
            if fr:
                failure_reason = fr
                break

    sep = "─" * 64
    typer.echo(
        f"\n{_b(f'── {task_id} ({status})')} {sep[: max(0, 60 - len(task_id) - len(status))]}"
    )
    typer.echo(f"  {_b('Last failure :')} {failure_reason}")
    typer.echo(f"  {_b('Retries used :')} {retry_count}/{max_retries}")

    typer.echo(f"\n{_b('── Last run log')} (tail {lines}) {'─' * 44}")
    if log_path and Path(log_path).exists():
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines()[-lines:]:
            typer.echo(f"  {line}")
    else:
        typer.echo(f"  {_d('(no run log found)')}")

    typer.echo(f"\n{_b('── Sandbox git status')} {'─' * 42}")
    if repo:
        try:
            result = _sp.run(
                ["git", "-C", repo, "status", "--short", "--branch"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in (result.stdout or result.stderr or "(empty)").splitlines():
                typer.echo(f"  {line}")
        except Exception as exc:
            typer.echo(f"  {_d(f'(git status failed: {exc})')}")
    else:
        typer.echo(f"  {_d('(pass --repo or set SANDBOX_REPO_PATH to see git state)')}")
    typer.echo("")


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
# Agent identity commands
# ---------------------------------------------------------------------------

_HUMAN_TAUGHT_PREFIXES = ("skill/human/", "skill/session/")


def _get_llm_backend() -> str:
    """Return the active LLM backend: 'claude' (default) or 'python'."""
    val = os.getenv("ORCHESTRA_LLM_BACKEND", "").lower()
    if val in {"claude", "python"}:
        return val
    cfg = Path.home() / ".config" / "orchestra" / "config"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.startswith("llm_backend="):
                val = line.split("=", 1)[1].strip().lower()
                if val in {"claude", "python"}:
                    return val
    return "claude"


# ---------------------------------------------------------------------------
# config sub-app
# ---------------------------------------------------------------------------

_config_app = typer.Typer(help="Manage orchctl session configuration.")
app.add_typer(_config_app, name="config")

_CFG_PATH = Path.home() / ".config" / "orchestra" / "config"
_VALID_BACKENDS = {"claude", "python"}


@_config_app.command("show")
def config_show() -> None:
    """Show current orchctl configuration and backend availability."""
    import shutil

    # Determine backend and source
    env_val = os.getenv("ORCHESTRA_LLM_BACKEND", "").lower()
    if env_val in _VALID_BACKENDS:
        backend, source = env_val, "ORCHESTRA_LLM_BACKEND env var"
    else:
        file_val = ""
        if _CFG_PATH.exists():
            for line in _CFG_PATH.read_text().splitlines():
                if line.startswith("llm_backend="):
                    file_val = line.split("=", 1)[1].strip().lower()
        if file_val in _VALID_BACKENDS:
            backend, source = file_val, "~/.config/orchestra/config"
        else:
            backend, source = "claude", "default"

    typer.echo(f"\n  llm_backend = {_b(backend)}  ({source})\n")
    # Claude availability
    claude_ok = bool(shutil.which("claude"))
    typer.echo(
        f"  claude CLI   : {'available' if claude_ok else _r('not found — run: claude login')}"
    )
    # Python LLMClient availability
    api_key_set = bool(os.getenv("ANTHROPIC_API_KEY"))
    typer.echo(
        f"  python LLM   : {'ANTHROPIC_API_KEY set' if api_key_set else _r('ANTHROPIC_API_KEY not set')}"
    )
    typer.echo()


@_config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key, e.g. llm-backend"),
    value: str = typer.Argument(..., help="Value, e.g. claude or python"),
) -> None:
    """Set a configuration value (saved to ~/.config/orchestra/config)."""
    key = key.lower().replace("-", "_")
    value = value.lower()

    if key == "llm_backend":
        if value not in _VALID_BACKENDS:
            typer.echo(f"Error: invalid backend {value!r} — choose 'claude' or 'python'.", err=True)
            raise typer.Exit(1)
    else:
        typer.echo(f"Error: unknown config key {key!r}.", err=True)
        raise typer.Exit(1)

    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Read existing lines, update or append
    lines: list[str] = []
    if _CFG_PATH.exists():
        lines = _CFG_PATH.read_text().splitlines()
    prefix = f"{key}="
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}"
            updated = True
            break
    if not updated:
        lines.append(f"{prefix}{value}")
    _CFG_PATH.write_text("\n".join(lines) + "\n")
    typer.echo(f"  Set {key} = {_b(value)}  (saved to ~/.config/orchestra/config)")


def _is_human_taught(key: str) -> bool:
    return any(key.startswith(p) for p in _HUMAN_TAUGHT_PREFIXES)


def _build_session_prompt(agent_id: str, memories: list[dict]) -> str:
    """Assemble a system prompt for orchctl ask / orchctl session (LLMClient path)."""
    identity = next((m for m in memories if m["memory_type"] == "identity"), None)
    episodes = [m for m in memories if m["memory_type"] == "episode"]
    skills = [m for m in memories if m["memory_type"] == "skill"]

    identity_text = identity["content"] if identity else "(no identity memory yet)"
    recent_eps = episodes[-3:]
    ep_text = "\n\n".join(m["content"][:200] for m in recent_eps) or "(none)"
    skill_text = "\n\n".join(m["content"] for m in skills)[:1200] or "(none)"

    return (
        f"You are {agent_id} — an AI agent for this software project.\n\n"
        "## IDENTITY SESSION MODE\n"
        "You are NOT executing a task. There is no branch to commit to, no deliverables, "
        "no gateway calls needed.\n"
        "This session is for identity work: reflecting on your expertise, accepting new "
        "skills from the operator, and exploring your knowledge. "
        "Respond naturally and conversationally.\n\n"
        f"{identity_text}\n\n"
        "## Task history\n"
        f"{len(episodes)} tasks completed.\n"
        f"Recent episodes:\n{ep_text}\n\n"
        "## Skills\n"
        f"{skill_text}"
    )


def _build_cli_session_prompt(agent_id: str, memories: list[dict]) -> str:
    """System prompt for the claude CLI subprocess path.

    Extends the base prompt with curl instructions so the agent can write
    skills and search memories via the orchestrator REST API (no capability
    token required — POST /agent-memories is platform-open).
    """
    base = _build_session_prompt(agent_id, memories)
    orch_url = _URL  # same base URL used by _client()
    return (
        base + f"\n\n## Recording a skill during this session\n"
        "When you learn something worth keeping, run this curl command:\n"
        f"  curl -s -X POST {orch_url}/agent-memories \\\n"
        "    -H 'Content-Type: application/json' \\\n"
        f'    -d \'{{"agent_id": "{agent_id}", "memory_type": "skill", '
        '"key": "skill/session/<topic>", '
        '"content": "<what to remember>", "actor": "session"}}\'\n\n'
        "## Searching your memories\n"
        f"  curl -s '{orch_url}/agent-memories?agent_id={agent_id}'"
    )


def _claude_ask(agent_id: str, question: str, system: str) -> None:
    """Run a one-shot ask via the `claude` CLI subprocess."""
    import shutil
    import subprocess

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("Error: 'claude' CLI not found. Run 'claude login' first.", err=True)
        raise typer.Exit(1)

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["claude", "--system-prompt", system, "-p", question],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout.strip() or result.stderr.strip()
    typer.echo(f"\n{output}\n")


def _claude_session(agent_id: str, system: str) -> None:
    """Run an interactive identity session via the `claude` CLI subprocess."""
    import shutil
    import subprocess

    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("Error: 'claude' CLI not found. Run 'claude login' first.", err=True)
        raise typer.Exit(1)

    typer.echo(
        f"\n  {_b('[identity session: ' + agent_id + ']')}  "
        "Launching claude CLI — type 'exit' inside to end.\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    subprocess.run(["claude", "--system-prompt", system], env=env)


_SESSION_TOOLS = [
    {
        "name": "write_skill",
        "description": "Record a new skill or fact to retain for future tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Short topic slug, e.g. 'db-pattern'."},
                "content": {"type": "string", "description": "The skill or fact to remember."},
            },
            "required": ["topic", "content"],
        },
    },
    {
        "name": "search_memory",
        "description": "Search memory archive by keyword.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search keywords."}},
            "required": ["query"],
        },
    },
    {
        "name": "end_session",
        "description": "End this identity session.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _llm_ask(agent_id: str, question: str, system: str, model: str) -> None:
    """One-shot ask via LLMClient (Python backend)."""
    if not _HAS_LLM:
        typer.echo("Error: LLM client not available (missing agents/shared package).", err=True)
        raise typer.Exit(1)
    llm = _LLMClient(model=model)
    try:
        response = llm.call(
            messages=[{"role": "user", "content": question}], system=system, max_tokens=1024
        )
    except Exception as exc:
        typer.echo(f"Error calling LLM: {exc}", err=True)
        raise typer.Exit(1)
    for block in response.content:
        if hasattr(block, "text"):
            typer.echo(f"\n{block.text}\n")


def _llm_session(agent_id: str, memories: list[dict], system: str, model: str) -> None:
    """Multi-turn identity session via LLMClient (Python backend)."""
    if not _HAS_LLM:
        typer.echo("Error: LLM client not available (missing agents/shared package).", err=True)
        raise typer.Exit(1)
    llm = _LLMClient(model=model)
    messages: list[dict] = []
    typer.echo(f"\n  {_b('[identity session: ' + agent_id + ']')}  Type 'exit' or Ctrl+D to end.\n")
    while True:
        try:
            user_input = typer.prompt(f"  {_b('You')}").strip()
        except (typer.Abort, EOFError, KeyboardInterrupt):
            typer.echo("\n  Session ended.")
            break
        if user_input.lower() in {"exit", "quit", "bye"}:
            typer.echo("  Session ended.")
            break
        messages.append({"role": "user", "content": user_input})
        try:
            response = llm.call(
                messages=messages, system=system, tools=_SESSION_TOOLS, max_tokens=2048
            )
        except Exception as exc:
            typer.echo(f"  {_r('LLM error:')} {exc}", err=True)
            break
        reply_text = ""
        tool_results = []
        for block in response.content:
            if hasattr(block, "text"):
                reply_text += block.text
            elif block.type == "tool_use":
                name, inp = block.name, block.input
                if name == "end_session":
                    if reply_text:
                        typer.echo(f"\n  {_b(agent_id + ':')} {reply_text}")
                    typer.echo("  Session ended.")
                    return
                elif name == "write_skill":
                    topic = inp.get("topic", "general")
                    content = inp.get("content", "")
                    with _client() as c:
                        r = c.post(
                            "/agent-memories",
                            json={
                                "agent_id": agent_id,
                                "memory_type": "skill",
                                "key": f"skill/session/{topic}",
                                "content": content,
                                "actor": "session",
                            },
                        )
                    tool_out = f"Skill recorded: {topic}" if not r.is_error else f"Error: {r.text}"
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": tool_out}
                    )
                elif name == "search_memory":
                    query = inp.get("query", "").lower()
                    hits = [m for m in memories if query in m.get("content", "").lower()][:5]
                    tool_out = (
                        "\n".join(
                            f"[{h['memory_type']}] {h['key']}: {h['content'][:150]}" for h in hits
                        )
                        if hits
                        else "No memories found."
                    )
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": tool_out}
                    )
        if reply_text:
            typer.echo(f"\n  {_b(agent_id + ':')} {reply_text}\n")
        messages.append({"role": "assistant", "content": response.content})
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
            try:
                follow = llm.call(
                    messages=messages, system=system, tools=_SESSION_TOOLS, max_tokens=2048
                )
                follow_text = "".join(b.text for b in follow.content if hasattr(b, "text"))
                if follow_text:
                    typer.echo(f"  {_b(agent_id + ':')} {follow_text}\n")
                messages.append({"role": "assistant", "content": follow.content})
            except Exception as exc:
                typer.echo(f"  {_r('LLM error:')} {exc}", err=True)


@app.command("identities")
def identities(
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Filter by agent ID."),
) -> None:
    """List agent identity profiles with task history and skill breakdown."""
    params: dict = {"project_id": "default"}
    if agent:
        params["agent_id"] = agent

    with _client() as c:
        all_rows = c.get("/agent-memories", params=params).json()

    by_type: dict[str, list] = {"identity": [], "episode": [], "skill": [], "convention": []}
    for m in all_rows if isinstance(all_rows, list) else []:
        t = m.get("memory_type", "")
        if t in by_type:
            by_type[t].append(m)

    agent_ids: list[str] = []
    seen: set[str] = set()
    for m in by_type["identity"]:
        if m["agent_id"] not in seen:
            seen.add(m["agent_id"])
            agent_ids.append(m["agent_id"])
    # Also include agents with episodes/skills but no identity yet
    for lst in (by_type["episode"], by_type["skill"]):
        for m in lst:
            if m["agent_id"] not in seen:
                seen.add(m["agent_id"])
                agent_ids.append(m["agent_id"])

    if not agent_ids:
        typer.echo("  No agent profiles found.")
        return

    for aid in sorted(agent_ids):
        identity_row = next((m for m in by_type["identity"] if m["agent_id"] == aid), None)
        episodes = [m for m in by_type["episode"] if m["agent_id"] == aid]
        skills = [m for m in by_type["skill"] if m["agent_id"] == aid]

        typer.echo(f"\n  {_b(aid)}")
        if identity_row:
            updated = identity_row.get("updated_at", "")[:19]
            typer.echo(f"  Last updated: {_d(updated)}")
            content = identity_row["content"]
            # Extract role line
            if "## Role" in content:
                role_body = content.split("## Role", 1)[1].strip()
                role_line = next((ln.strip() for ln in role_body.splitlines() if ln.strip()), "")
                typer.echo(f"  Role: {_d(role_line[:100])}")
            # Extract domain expertise
            if "## Domain expertise" in content:
                exp_body = content.split("## Domain expertise", 1)[1]
                next_sec = exp_body.find("\n## ")
                exp_body = exp_body[:next_sec].strip() if next_sec != -1 else exp_body.strip()
                if exp_body and exp_body != "(none yet — updated as tasks complete)":
                    tags = [
                        ln[2:].split(" (")[0].strip()
                        for ln in exp_body.splitlines()
                        if ln.startswith("- ")
                    ]
                    typer.echo(f"  Domain: {_d(', '.join(tags))}")
        else:
            typer.echo(f"  {_d('(no identity memory)')}")

        typer.echo(f"  Tasks completed: {len(episodes)}")

        auto_skills = [s for s in skills if not _is_human_taught(s["key"])]
        human_skills = [s for s in skills if _is_human_taught(s["key"])]
        typer.echo(f"  Skills: {len(auto_skills)} auto, {len(human_skills)} human-taught")

        if human_skills:
            for s in human_skills:
                topic = s["key"].split("/")[-1]
                snippet = s["content"][:60].replace("\n", " ")
                typer.echo(
                    f"    {_b('[human-taught]')} {topic:<20} {_d(snippet)}  "
                    f"{_d('[id: ' + s['id'][:8] + ']')}"
                )


@app.command("teach")
def teach(
    agent_id: str = typer.Argument(..., help="Agent ID, e.g. claude-code-agent."),
    fact: str = typer.Argument(..., help="Skill or fact to record."),
    topic: str = typer.Option(
        "general", "--topic", "-t", help="Short topic slug (e.g. db-pattern)."
    ),
) -> None:
    """Inject a skill or fact directly into an agent's memory.

    \b
    Use 'orchctl forget AGENT-ID TOPIC' to remove it later.
    Re-teaching the same topic overwrites the previous content.
    """
    key = f"skill/human/{topic}"
    with _client() as c:
        resp = c.post(
            "/agent-memories",
            json={
                "agent_id": agent_id,
                "memory_type": "skill",
                "key": key,
                "content": fact,
                "actor": "human",
            },
        )
    _handle_error(resp)
    typer.echo(
        f"  Wrote [human-taught skill] to {agent_id}: topic={topic}\n"
        f"  Remove with: orchctl forget {agent_id} {topic}"
    )


@app.command("forget")
def forget(
    agent_id: str = typer.Argument(..., help="Agent ID, e.g. claude-code-agent."),
    topic_or_id: str = typer.Argument(..., help="Topic slug or 8-char memory ID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove a human-taught skill from an agent's memory.

    \b
    Matches by topic slug (last segment of key) or 8-char memory ID prefix.
    Only removes entries written via 'orchctl teach' or an identity session.
    """
    with _client() as c:
        resp = c.get("/agent-memories", params={"agent_id": agent_id, "memory_type": "skill"})
    _handle_error(resp)
    skills = resp.json()
    human = [s for s in skills if _is_human_taught(s["key"])]

    match = next(
        (
            s
            for s in human
            if s["id"].startswith(topic_or_id) or s["key"].split("/")[-1] == topic_or_id
        ),
        None,
    )
    if match is None:
        typer.echo(
            f"  No human-taught skill matching {topic_or_id!r} found for {agent_id}.",
            err=True,
        )
        raise typer.Exit(1)

    if not yes:
        typer.echo(f"\n  Will delete: {match['key']}  ({match['content'][:60]})")
        typer.confirm("  Confirm deletion?", abort=True)

    with _client() as c:
        resp = c.request(
            "DELETE",
            f"/agent-memories/{match['id']}",
            json={"reason": "human removed via orchctl forget"},
        )
    _handle_error(resp)
    typer.echo(f"  Removed skill {match['key']!r} from {agent_id}.")


@app.command("ask")
def ask(
    agent_id: str = typer.Argument(
        ..., help="Agent ID whose identity to probe, e.g. backend-agent."
    ),
    question: str = typer.Argument(..., help="Question to ask."),
    model: str = typer.Option(
        "claude-haiku-4-5-20251001", "--model", "-m", help="Model to use (python backend only)."
    ),
) -> None:
    """One-shot competency probe — ask a question grounded in an agent's identity memory.

    \b
    Backend is set with 'orchctl config set llm-backend <claude|python>'.
    claude (default): uses the claude CLI subprocess (needs 'claude login').
    python: uses LLMClient directly (needs ANTHROPIC_API_KEY).
    """
    with _client() as c:
        resp = c.get("/agent-memories", params={"agent_id": agent_id})
    _handle_error(resp)
    memories = resp.json()
    if _get_llm_backend() == "python":
        _llm_ask(agent_id, question, _build_session_prompt(agent_id, memories), model)
    else:
        _claude_ask(agent_id, question, _build_cli_session_prompt(agent_id, memories))


@app.command("session")
def session_cmd(
    agent_id: str = typer.Argument(
        ..., help="Agent ID whose identity to embody, e.g. backend-agent."
    ),
    model: str = typer.Option(
        "claude-sonnet-4-6", "--model", "-m", help="Model to use (python backend only)."
    ),
) -> None:
    """Multi-turn interactive identity session with an agent.

    \b
    Backend is set with 'orchctl config set llm-backend <claude|python>'.
    claude (default): uses the claude CLI subprocess (needs 'claude login').
    python: uses LLMClient directly (needs ANTHROPIC_API_KEY).
    The agent can search its memory and write new skills during the session.
    """
    with _client() as c:
        resp = c.get("/agent-memories", params={"agent_id": agent_id})
    _handle_error(resp)
    memories: list[dict] = resp.json()
    if _get_llm_backend() == "python":
        _llm_session(agent_id, memories, _build_session_prompt(agent_id, memories), model)
    else:
        _claude_session(agent_id, _build_cli_session_prompt(agent_id, memories))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
