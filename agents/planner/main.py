"""Planner agent — reads a spec file and submits tasks to the orchestrator.

Calls Claude once with the spec content; Claude returns a JSON task plan.
The planner creates each task via the orchestrator API, wires depends_on using
the IDs returned during creation, then approves root tasks to start execution.

Usage:
    uv run python -m agents.planner.main \\
        --spec diary_spec.md \\
        --repo ./sandbox/sample-project \\
        [--orchestrator-url http://localhost:8080]
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv

from agents.shared.llm import LLMClient

load_dotenv()

app = typer.Typer(name="planner", no_args_is_help=True)

_SYSTEM_PROMPT = """\
You are a project planner for the Orchestra multi-agent orchestration platform.
Read the specification below and decompose the work into tasks for three agents:

  backend-agent   — server-side: APIs, data models, business logic, tests
  frontend-agent  — client-side: HTML, CSS, JavaScript, single-page UI
  qa-agent        — quality: test plans, QA reports, risk assessment

Return ONLY a JSON array — no explanation, no markdown code fences. Each element:
{
  "title":      "<short imperative phrase>",
  "owner":      "backend-agent" | "frontend-agent" | "qa-agent",
  "depends_on": ["<exact title of another task in this list>"],
  "inputs":     ["<repo-relative file path this task reads>"],
  "outputs":    ["<repo-relative file path this task writes>"],
  "acceptance": ["<one acceptance criterion per string>"]
}

Rules:
- backend-agent tasks have no depends_on (they are always roots).
- frontend-agent and qa-agent tasks depend on the backend-agent task whose outputs
  they consume — list those backend task titles in their depends_on.
- Keep the plan to 3–5 tasks total; do not split work an agent can handle internally.
- Do not include risk_tier; the planner will set it to 1 for all tasks.
"""


def _topo_sort(tasks: list[dict]) -> list[dict]:
    """Return tasks in an order where every task appears after its dependencies."""
    by_title = {t["title"]: t for t in tasks}
    visited: set[str] = set()
    result: list[dict] = []

    def visit(t: dict) -> None:
        if t["title"] in visited:
            return
        for dep in t.get("depends_on", []):
            if dep in by_title:
                visit(by_title[dep])
        visited.add(t["title"])
        result.append(t)

    for t in tasks:
        visit(t)
    return result


def _parse_task_plan(text: str) -> list[dict]:
    """Extract and parse a JSON array from the LLM response."""
    text = text.strip()
    # Strip markdown code fences if the model added them despite instructions
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    return json.loads(text.strip())


@app.command()
def main(
    spec: str = typer.Option(..., "--spec", "-s", help="Spec file path relative to repo root."),
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
    """Read a spec file and submit a task plan to the orchestrator."""
    repo_path = Path(repo or os.getenv("SANDBOX_REPO_PATH", "./sandbox/sample-project")).resolve()
    orch_url = orchestrator_url or os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")

    spec_file = repo_path / spec
    if not spec_file.exists():
        typer.echo(f"ERROR: spec file not found: {spec_file}", err=True)
        raise typer.Exit(1)

    spec_text = spec_file.read_text()
    typer.echo(f"Spec: {spec_file} ({len(spec_text)} chars)")

    # ── 1. Ask Claude to decompose the spec ──────────────────────────────────
    typer.echo("Calling LLM to decompose spec into tasks...")
    llm = LLMClient()
    response = llm.call(
        messages=[{"role": "user", "content": spec_text}],
        system=_SYSTEM_PROMPT,
        run_id=None,
        session=None,
        max_tokens=2048,
    )

    raw = response.content[0].text
    try:
        plan = _parse_task_plan(raw)
    except (json.JSONDecodeError, IndexError) as exc:
        typer.echo(f"ERROR: LLM did not return valid JSON: {exc}", err=True)
        typer.echo(f"Raw response:\n{raw}", err=True)
        raise typer.Exit(1)

    if not isinstance(plan, list) or not plan:
        typer.echo("ERROR: LLM returned an empty or non-list task plan.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Plan: {len(plan)} tasks")

    # ── 2. Create tasks in dependency order ───────────────────────────────────
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

        # ── 3. Approve root tasks (no depends_on) ─────────────────────────────
        typer.echo("")
        typer.echo("Approving root tasks (created → assigned)...")

        for task_def in ordered:
            if task_def.get("depends_on"):
                continue  # skip tasks that depend on others; they'll be unblocked by dispatcher

            task_id = title_to_id[task_def["title"]]
            resp = client.post(
                f"/tasks/{task_id}/transition",
                json={"new_status": "assigned", "actor": "planner"},
            )
            if resp.is_error:
                typer.echo(f"ERROR approving {task_id}: {resp.status_code} {resp.text}", err=True)
                raise typer.Exit(1)
            typer.echo(f"  Approved {task_id} → assigned")

    # ── 4. Summary ────────────────────────────────────────────────────────────
    typer.echo("")
    typer.echo("Task plan submitted:")
    typer.echo("")

    col_id = max(len("ID"), max(len(i) for i in title_to_id.values()))
    col_ow = max(len("OWNER"), max(len(t["owner"]) for t in plan))
    col_st = len("STATUS")
    fmt = f"  {{:<{col_id}}}  {{:<{col_ow}}}  {{:<{col_st}}}  {{}}"

    typer.echo(fmt.format("ID", "OWNER", "STATUS", "TITLE"))
    typer.echo("  " + "-" * (col_id + col_ow + col_st + 40))
    for task_def in ordered:
        tid = title_to_id[task_def["title"]]
        status = "assigned" if not task_def.get("depends_on") else "created"
        typer.echo(fmt.format(tid, task_def["owner"], status, task_def["title"]))

    typer.echo("")
    typer.echo("Root tasks are now 'assigned' — the dispatcher will launch agents automatically.")
    typer.echo("Downstream tasks will be unblocked when their dependencies complete.")
    typer.echo("")
    typer.echo("Monitor progress:  uv run orchctl list")
    typer.echo("Tail logs      :   make logs")


if __name__ == "__main__":
    app()
