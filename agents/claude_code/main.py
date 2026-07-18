"""Claude Code agent wrapper.

Launches the `claude` CLI as a subprocess to execute a task. The wrapper:
  1. Creates the agent branch via the gateway.
  2. Runs `claude --dangerously-skip-permissions -p "<instruction>"` in the repo.
  3. On success: commits changed files via the gateway and marks the task completed.
  4. On failure: marks the task failed.

Usage:
    uv run python -m agents.claude_code.main \\
        --context /tmp/orchestra/runs/<run_id>.json \\
        --run-id <uuid> \\
        [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = typer.Typer(name="claude-code-agent", add_completion=False)


def _build_instruction(pkg: dict, repo_path: str) -> str:
    task = pkg["task"]
    task_owner: str = task.get("owner", "claude-code-agent")
    branch = pkg["agent_instructions"]["branch"]

    inputs_list = "\n".join(f"- {p}" for p in task["inputs"]) if task["inputs"] else "(none)"
    outputs_list = "\n".join(f"- {p}" for p in task["outputs"]) if task["outputs"] else "(none)"
    acceptance_list = (
        "\n".join(f"- {c}" for c in task["acceptance"]) if task["acceptance"] else "(none)"
    )

    # Inline the pre-loaded input artifact content so Claude doesn't need extra reads.
    artifact_section = ""
    for art in pkg.get("input_artifacts", []):
        if art.get("found") and art.get("content"):
            artifact_section += f"\n### {art['path']}\n\n```\n{art['content']}\n```\n"

    # Format agent memory section if present.
    memory_section = ""
    mem = pkg.get("agent_memory")
    if mem:
        parts = ["\n## Your memory (read this before starting)\n"]
        if mem.get("_warning"):
            parts.append(f"> WARNING: {mem['_warning']}\n")
        if mem.get("identity"):
            parts.append(f"### Identity\n{mem['identity']}\n")
        if mem.get("episodes"):
            parts.append("### Past episodes")
            for ep in mem["episodes"]:
                parts.append(ep)
            parts.append("")
        if mem.get("skills"):
            parts.append("### Acquired skills")
            for sk in mem["skills"]:
                parts.append(sk)
            parts.append("")
        if mem.get("shared_skills"):
            parts.append("### Shared project conventions (all agents)")
            for sk in mem["shared_skills"]:
                parts.append(sk)
            parts.append("")
        parts.append(
            "If you discover a reusable project convention (not task-specific detail), "
            "record it:\n"
            "  curl -s -X POST http://localhost:8081/memory/upsert \\\n"
            "    -H 'Content-Type: application/json' \\\n"
            f'    -d \'{{"task_id":"{task["id"]}","project_id":"default",'
            '"memory_type":"skill","topic":"<slug>","content":"<under 200 words, no file dumps>"}\'\n\n'
            "To search your memory archive for a keyword:\n"
            "  curl -s -X POST http://localhost:8081/memory/search \\\n"
            "    -H 'Content-Type: application/json' \\\n"
            f'    -d \'{{"task_id":"{task["id"]}","query":"<keyword>","max_results":5}}\''
        )
        memory_section = "\n".join(parts)

    return f"""\
You are acting as {task_owner} for this project.
You are working on a software development task in the Git repository at {repo_path}.
Your work will be committed to branch `{branch}` (already checked out for you).

## Task

{task["title"]}

## Acceptance criteria

{acceptance_list}

## Output files (create or modify these)

{outputs_list}

## Input files

{inputs_list}
{artifact_section}{memory_section}

## Rules

- Work only within {repo_path}. Do not create files outside it.
- Do NOT run `git commit`, `git push`, or `git branch`. Orchestra handles git after you finish.
- When all acceptance criteria are satisfied, you are done -- exit cleanly.
- Ensure `ruff check .` passes with zero errors on all Python files you write.
- If tests are expected, make them pass under `pytest`.
"""


def _call(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    resp = client.request(method, url, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _mark_failed(http: httpx.Client, orch_url: str, task_id: str) -> None:
    try:
        http.post(
            f"{orch_url}/tasks/{task_id}/transition",
            json={"new_status": "failed", "actor": "claude-code-agent"},
        )
        log.info("Task %s marked failed", task_id)
    except Exception as exc:
        log.warning("Could not mark task %s as failed: %s", task_id, exc)


@app.command()
def main(
    context: str = typer.Option(..., "--context", "-c", help="Path to context package JSON."),
    run_id: str = typer.Option(..., "--run-id", help="Run UUID."),
    repo: str = typer.Option(None, "--repo", "-r", help="Managed repo path."),
    gateway_url: str = typer.Option(None, "--gateway-url", help="Gateway base URL."),
    orchestrator_url: str = typer.Option(None, "--orchestrator-url", help="Orchestrator base URL."),
) -> None:
    """Run the Claude Code CLI agent for a given context package."""
    pkg = json.loads(Path(context).read_text(encoding="utf-8"))
    task_id: str = pkg["task_id"]
    task_owner: str = pkg["task"]["owner"]
    branch: str = pkg["agent_instructions"]["branch"]
    commit_prefix: str = pkg["agent_instructions"].get("commit_prefix", f"[{task_id}]")

    repo_path = repo or os.getenv("SANDBOX_REPO_PATH", "./sandbox/sample-project")
    gw_url = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8081")
    orch_url = orchestrator_url or os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")

    log.info("Claude Code agent starting: task=%s branch=%s", task_id, branch)

    with httpx.Client(timeout=30.0) as http:
        # ── 1. Create branch via gateway ─────────────────────────────────────
        try:
            _call(
                http,
                "POST",
                f"{gw_url}/git/branch",
                json={
                    "agent_id": task_owner,
                    "task_id": task_id,
                    "repo_path": repo_path,
                    "branch": branch,
                },
            )
            log.info("Branch created: %s", branch)
        except httpx.HTTPStatusError as exc:
            log.error("Failed to create branch: %s", exc.response.text)
            _mark_failed(http, orch_url, task_id)
            raise typer.Exit(1)

        # ── 2. Run Claude Code ─────────────────────────────────────────────
        instruction = _build_instruction(pkg, repo_path)
        log.info("Launching claude CLI (timeout 1800s)...")

        # Drop ANTHROPIC_API_KEY so claude uses its own session auth, not API-key mode.
        claude_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        try:
            result = subprocess.run(
                ["claude", "--dangerously-skip-permissions", "-p", instruction],
                cwd=repo_path,
                env=claude_env,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except FileNotFoundError:
            log.error("'claude' CLI not found. Install Claude Code and run `claude login`.")
            _mark_failed(http, orch_url, task_id)
            raise typer.Exit(1)
        except subprocess.TimeoutExpired:
            log.error("claude CLI timed out after 1800s")
            _mark_failed(http, orch_url, task_id)
            raise typer.Exit(1)

        if result.returncode != 0:
            log.error("claude CLI exited %d:\n%s", result.returncode, result.stderr[:2000])
            _mark_failed(http, orch_url, task_id)
            raise typer.Exit(1)

        log.info("claude CLI finished successfully")

        # ── 3. Collect changed files ───────────────────────────────────────
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            text=True,
        )
        changed_paths = [
            line[3:].strip()
            for line in status_out.splitlines()
            if line.strip() and "__pycache__" not in line and not line[3:].strip().endswith(".pyc")
        ]

        if not changed_paths:
            log.warning("claude exited 0 but no files changed; marking task failed")
            _mark_failed(http, orch_url, task_id)
            raise typer.Exit(1)

        log.info("Changed files: %s", changed_paths)

        # ── 4. Commit via gateway ─────────────────────────────────────────
        try:
            resp = _call(
                http,
                "POST",
                f"{gw_url}/git/commit",
                json={
                    "agent_id": task_owner,
                    "task_id": task_id,
                    "repo_path": repo_path,
                    "message": f"{commit_prefix} {task_owner} output",
                    "paths": changed_paths,
                },
            )
            log.info("Committed: sha=%s", resp.get("sha", "?"))
        except httpx.HTTPStatusError as exc:
            log.error("Commit failed: %s", exc.response.text)
            _mark_failed(http, orch_url, task_id)
            raise typer.Exit(1)

        # ── 5. Transition task to completed ───────────────────────────────
        try:
            _call(
                http,
                "POST",
                f"{orch_url}/tasks/{task_id}/transition",
                json={"new_status": "completed", "actor": "claude-code-agent"},
            )
            log.info("Task %s marked completed", task_id)
        except httpx.HTTPStatusError as exc:
            log.error("Transition to completed failed: %s", exc.response.text)
            raise typer.Exit(1)

        # ── 6. Write skill memory ──────────────────────────────────────────
        task_title = pkg.get("task", {}).get("title", task_id)
        files_summary = ", ".join(changed_paths[:20]) or "(none)"
        skill_content = (f"Task: {task_title}\nFiles produced: {files_summary}\nBranch: {branch}")[
            :2000
        ]
        try:
            _call(
                http,
                "POST",
                f"{gw_url}/memory/upsert",
                json={
                    "task_id": task_id,
                    "agent_id": task_owner,
                    "project_id": "default",
                    "memory_type": "skill",
                    "key": f"skill/{task_id}",
                    "content": skill_content,
                },
                headers={"X-Platform-Actor": task_owner},
            )
            log.info("Skill memory written for task %s", task_id)
        except Exception as exc:
            log.warning("Skill memory write failed (non-fatal): %s", exc)

    log.info("Claude Code agent done: task=%s", task_id)


if __name__ == "__main__":
    app()
