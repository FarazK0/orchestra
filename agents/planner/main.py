"""Planner agent — reads a spec or pre-built plan and submits tasks to the orchestrator.

Two modes:
  --spec PATH   Call the LLM to decompose a spec file into tasks (auto planning).
  --plan PATH   Load a pre-generated JSON task list (skip the LLM call entirely).

Usage:
    uv run python -m agents.planner.main --spec diary_spec.md
    uv run python -m agents.planner.main --plan tasks.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv

from agents.planner.plan_utils import (
    CHANGE_REQUEST_SYSTEM_PROMPT,
    build_snapshot,
    parse_task_plan,
    topo_sort,
)
from agents.shared.llm import LLMClient

load_dotenv()

app = typer.Typer(name="planner", no_args_is_help=True)

_SYSTEM_PROMPT = CHANGE_REQUEST_SYSTEM_PROMPT

# Aliases so existing internal call-sites below continue to work.
_topo_sort = topo_sort
_parse_task_plan = parse_task_plan


def _submit(plan: list[dict], orch_url: str) -> None:
    """Create tasks and approve roots via the orchestrator API."""
    ordered = _topo_sort(plan)
    title_to_id: dict[str, str] = {}

    with httpx.Client(base_url=orch_url, timeout=15.0) as client:
        for task_def in ordered:
            depends_on_ids = [
                title_to_id[t] for t in task_def.get("depends_on", []) if t in title_to_id
            ]
            payload = {
                "title": task_def["title"],
                "owner": task_def["owner"],
                "depends_on": depends_on_ids,
                "inputs": task_def.get("inputs", []),
                "outputs": task_def.get("outputs", []),
                "acceptance": task_def.get("acceptance", []),
                "risk_tier": 1,
            }
            resp = client.post("/tasks", json=payload)
            if resp.is_error:
                typer.echo(
                    f"ERROR creating task {task_def['title']!r}: {resp.status_code} {resp.text}",
                    err=True,
                )
                raise typer.Exit(1)
            created = resp.json()
            title_to_id[task_def["title"]] = created["id"]
            typer.echo(f"  Created {created['id']}: {created['title']!r}  [{created['owner']}]")

        typer.echo("")
        typer.echo("Approving root tasks (created -> assigned)...")
        for task_def in ordered:
            if task_def.get("depends_on"):
                continue
            task_id = title_to_id[task_def["title"]]
            resp = client.post(
                f"/tasks/{task_id}/transition",
                json={"new_status": "assigned", "actor": "planner"},
            )
            if resp.is_error:
                typer.echo(f"ERROR approving {task_id}: {resp.status_code} {resp.text}", err=True)
                raise typer.Exit(1)
            typer.echo(f"  Approved {task_id} -> assigned")

    typer.echo("")
    typer.echo("Task plan submitted:")
    typer.echo("")
    col_id = max(len("ID"), max(len(i) for i in title_to_id.values()))
    col_ow = max(len("OWNER"), max(len(t["owner"]) for t in plan))
    fmt = f"  {{:<{col_id}}}  {{:<{col_ow}}}  {{:<6}}  {{}}"
    typer.echo(fmt.format("ID", "OWNER", "STATUS", "TITLE"))
    typer.echo("  " + "-" * (col_id + col_ow + 50))
    for task_def in ordered:
        tid = title_to_id[task_def["title"]]
        status = "assigned" if not task_def.get("depends_on") else "created"
        typer.echo(fmt.format(tid, task_def["owner"], status, task_def["title"]))
    typer.echo("")
    typer.echo("Root tasks are 'assigned' -- the dispatcher will launch agents automatically.")
    typer.echo("Downstream tasks unblock when their dependencies complete.")
    typer.echo("")
    typer.echo("Monitor:  uv run orchctl list")
    typer.echo("Logs   :  make logs")


@app.command()
def main(
    spec: str = typer.Option(
        None,
        "--spec",
        "-s",
        help="Spec file path relative to repo root. Orchestra calls the LLM to decompose it.",
    ),
    plan: str = typer.Option(
        None,
        "--plan",
        "-p",
        help="Path to a pre-generated JSON task plan. Skips the LLM call entirely.",
    ),
    repo: str = typer.Option(
        None,
        "--repo",
        "-r",
        help="Managed repo path. Defaults to $SANDBOX_REPO_PATH or ./sandbox/sample-project.",
    ),
    orchestrator_url: str = typer.Option(
        None,
        "--orchestrator-url",
        help="Orchestrator base URL. Defaults to $ORCHESTRATOR_URL or http://localhost:8080.",
    ),
) -> None:
    """Submit a task plan to the orchestrator from a spec file or a pre-built JSON plan."""
    if not spec and not plan:
        typer.echo("ERROR: Provide --spec <path> or --plan <path>.", err=True)
        raise typer.Exit(1)
    if spec and plan:
        typer.echo("ERROR: Provide either --spec or --plan, not both.", err=True)
        raise typer.Exit(1)

    repo_path = Path(repo or os.getenv("SANDBOX_REPO_PATH", "./sandbox/sample-project")).resolve()
    orch_url = orchestrator_url or os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")

    # ── Load the plan ─────────────────────────────────────────────────────────
    if plan:
        plan_file = Path(plan)
        if not plan_file.exists():
            typer.echo(f"ERROR: plan file not found: {plan_file}", err=True)
            raise typer.Exit(1)
        raw = plan_file.read_text()
        typer.echo(f"Plan: {plan_file}")
    else:
        spec_file = repo_path / spec  # type: ignore[arg-type]
        if not spec_file.exists():
            typer.echo(f"ERROR: spec file not found: {spec_file}", err=True)
            raise typer.Exit(1)
        spec_text = spec_file.read_text()
        typer.echo(f"Spec: {spec_file} ({len(spec_text)} chars)")
        typer.echo("Calling LLM to decompose spec into tasks...")
        snapshot = build_snapshot(repo_path)
        user_content = f"## Project state\n\n{snapshot}\n\n## Change request\n\n{spec_text}"
        llm = LLMClient()
        response = llm.call(
            messages=[{"role": "user", "content": user_content}],
            system=_SYSTEM_PROMPT,
            run_id=None,
            session=None,
            max_tokens=2048,
        )
        raw = response.content[0].text

    try:
        task_plan = _parse_task_plan(raw)
    except (json.JSONDecodeError, IndexError) as exc:
        typer.echo(f"ERROR: Could not parse task plan as JSON: {exc}", err=True)
        typer.echo(f"Content:\n{raw}", err=True)
        raise typer.Exit(1)

    if not isinstance(task_plan, list) or not task_plan:
        typer.echo("ERROR: Task plan must be a non-empty JSON array.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Plan: {len(task_plan)} tasks")
    _submit(task_plan, orch_url)


if __name__ == "__main__":
    app()
