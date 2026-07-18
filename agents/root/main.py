"""Root agent -- persistent daemon that accepts change requests and dispatches sub-agents.

The root agent subscribes to the ``root:requests`` Redis stream.  When a change
request arrives (submitted via ``orchctl request``), it:

  1. Reads a snapshot of the managed repo (file tree + recent git log).
  2. Calls the planner (claude CLI or LLM, depending on AGENT_TYPE) to decompose
     the change into tasks.
  3. Creates the tasks in the orchestrator.
  4. Transitions root tasks (no depends_on) to ``assigned`` so the dispatcher
     picks them up immediately.

Merges are left to ``orchctl review`` (human approval gate, unchanged).

Usage:
    uv run python -m agents.root.main \\
        [--repo PATH] [--orchestrator-url URL] [--redis-url URL]
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv

from agents.planner.plan_utils import get_change_request_prompt, parse_task_plan, topo_sort
from agents.shared.llm import LLMClient
from orchestrator.orchestrator.db import get_engine, get_session_factory
from orchestrator.orchestrator.streams import ROOT_STREAM_KEY, StreamConsumer

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = typer.Typer(name="root-agent", add_completion=False)

# ---------------------------------------------------------------------------
# Project state snapshot
# ---------------------------------------------------------------------------


def _project_snapshot(repo_path: Path) -> str:
    """Return a compact text snapshot of the managed repo for the planner prompt."""
    lines: list[str] = []

    # Recent git log
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines.append("## Recent git history")
            lines.append(result.stdout.strip())
    except Exception:
        pass

    # File tree (non-hidden, non-pycache, max 100 files)
    try:
        result = subprocess.run(
            [
                "find",
                ".",
                "-not",
                "-path",
                "./.git*",
                "-not",
                "-path",
                "./__pycache__*",
                "-not",
                "-name",
                "*.pyc",
                "-not",
                "-path",
                "./.pytest_cache*",
                "-not",
                "-path",
                "./.ruff_cache*",
                "-type",
                "f",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            files = sorted(result.stdout.splitlines())[:100]
            lines.append("\n## Current file tree")
            lines.append("\n".join(files))
    except Exception:
        pass

    return "\n".join(lines) if lines else "(project state unavailable)"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _fetch_existing_tasks(orch_url: str) -> str:
    """Return a compact summary of all non-failed tasks for injection into the planner prompt."""
    import httpx

    try:
        with httpx.Client(base_url=orch_url, timeout=10.0) as client:
            tasks = client.get("/tasks").json()
    except Exception as exc:
        log.warning("Could not fetch existing tasks: %s", exc)
        return ""

    if not isinstance(tasks, list) or not tasks:
        return ""

    active_statuses = {
        "created",
        "assigned",
        "running",
        "completed",
        "validated",
        "merged",
        "closed",
    }
    lines = []
    for t in tasks:
        status = t.get("status", "")
        if status in active_statuses:
            lines.append(f"  [{t['id']}] ({status}) {t['title']}")

    if not lines:
        return ""
    return "## Existing tasks (do not re-create these)\n" + "\n".join(lines)


def _decompose_with_claude(
    description: str, spec_content: str, snapshot: str, existing_tasks: str, agent_type: str
) -> str:
    """Call the claude CLI to decompose a change request. Returns raw text."""
    system_prompt = get_change_request_prompt(agent_type)
    prompt = f"{system_prompt}\n\n## Project state\n\n{snapshot}\n\n"
    if existing_tasks:
        prompt += f"\n{existing_tasks}\n\n"
    prompt += f"## Change request\n\n{description}\n"
    if spec_content:
        prompt += f"\n## Additional spec\n\n{spec_content}\n"

    claude_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", prompt],
        env=claude_env,
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr[:500]}")
    return result.stdout


def _decompose_with_llm(
    description: str, spec_content: str, snapshot: str, existing_tasks: str, agent_type: str
) -> str:
    """Call the LLM API to decompose a change request. Returns raw text."""
    user_content = f"## Project state\n\n{snapshot}\n\n"
    if existing_tasks:
        user_content += f"\n{existing_tasks}\n\n"
    user_content += f"## Change request\n\n{description}\n"
    if spec_content:
        user_content += f"\n## Additional spec\n\n{spec_content}\n"

    llm = LLMClient()
    response = llm.call(
        messages=[{"role": "user", "content": user_content}],
        system=get_change_request_prompt(agent_type),
        run_id=None,
        session=None,
        max_tokens=2048,
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Identity memory seeding
# ---------------------------------------------------------------------------

_ROLE_DESCRIPTIONS: dict[str, str] = {
    "claude-code-agent": (
        "You are the generalist engineer for this project. "
        "You read existing code before writing, follow established patterns, and keep changes minimal. "
        "You handle any layer (backend, frontend, tests) as needed per task."
    ),
    "backend-agent": (
        "You are the backend specialist for this project. "
        "You own API routes, DB models, and business logic. "
        "You do not touch frontend files."
    ),
    "frontend-agent": (
        "You are the frontend specialist for this project. "
        "You own UI components, styles, and client-side logic. "
        "You do not touch backend or database files."
    ),
    "qa-agent": (
        "You are the QA specialist for this project. "
        "You write tests, check acceptance criteria, and verify correctness. "
        "You do not introduce new features — only validate existing ones."
    ),
}


def _role_for(agent_id: str) -> str:
    return _ROLE_DESCRIPTIONS.get(
        agent_id, f"You are a specialist agent ({agent_id}) for this project."
    )


def _seed_identity(
    agent_id: str,
    snapshot: str,
    description: str,
    orch_url: str,
    gateway_url: str,
    sample_task_id: str,
) -> None:
    """Seed or refresh identity memory for *agent_id* via the gateway.

    Skips if identity memory already exists and was updated after the
    10th-most-recent completed task for that agent (not yet stale).
    """
    import httpx

    # Check staleness: does identity exist and is it fresh?
    try:
        with httpx.Client(base_url=orch_url, timeout=10.0) as client:
            resp = client.get(
                "/agent-memories",
                params={"agent_id": agent_id, "memory_type": "identity"},
            )
            existing = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        log.warning("Could not check identity memory for %s: %s", agent_id, exc)
        existing = []

    if existing:
        # Count completed tasks for this agent since identity was last written.
        updated_at = existing[0].get("updated_at", "")
        try:
            with httpx.Client(base_url=orch_url, timeout=10.0) as client:
                completed = client.get("/tasks", params={"status": "closed"}).json()
            agent_completed = [
                t
                for t in completed
                if t.get("owner") == agent_id and t.get("updated_at", "") > updated_at
            ]
            if len(agent_completed) < 10:
                log.debug(
                    "Identity memory for %s is fresh (%d tasks since last seed)",
                    agent_id,
                    len(agent_completed),
                )
                return
        except Exception as exc:
            log.warning("Staleness check failed for %s: %s", agent_id, exc)
            return  # Don't re-seed if we can't check

    content = (
        f"## Role\n{_role_for(agent_id)}\n\n"
        f"## Project context\n{snapshot[:800]}\n\n"
        f"## Most recent change request\n{description[:400]}"
    )[:2000]

    try:
        with httpx.Client(base_url=gateway_url, timeout=10.0) as client:
            resp = client.post(
                "/memory/upsert",
                json={
                    "task_id": sample_task_id,
                    "agent_id": agent_id,
                    "project_id": "default",
                    "memory_type": "identity",
                    "key": "identity",
                    "content": content,
                },
                headers={"X-Platform-Actor": "root-agent"},
            )
            resp.raise_for_status()
        log.info("Seeded identity memory for %s", agent_id)
    except Exception as exc:
        log.warning("Failed to seed identity memory for %s: %s", agent_id, exc)


def _seed_shared_conventions(
    snapshot: str,
    description: str,
    gateway_url: str,
    sample_task_id: str,
) -> None:
    """Write project-level conventions into the shared memory pool (agent_id='shared').

    These are injected into every agent's context package under 'shared_skills',
    regardless of their type. Called once per change request.
    """
    content = (
        "## Project conventions\n"
        "- Follow existing file structure and naming conventions before introducing new ones.\n"
        "- Run `ruff check .` and `pytest` before calling task_complete.\n"
        "- Keep changes minimal and scoped to the task's output files.\n"
        "- Commit messages use the [TASK-ID] prefix provided in agent_instructions.\n\n"
        f"## Project snapshot\n{snapshot[:600]}\n\n"
        f"## Current change request\n{description[:300]}"
    )[:2000]

    try:
        with httpx.Client(base_url=gateway_url, timeout=10.0) as client:
            resp = client.post(
                "/memory/upsert",
                json={
                    "task_id": sample_task_id,
                    "agent_id": "shared",
                    "project_id": "default",
                    "memory_type": "convention",
                    "key": "shared/project-conventions",
                    "content": content,
                },
                headers={"X-Platform-Actor": "root-agent"},
            )
            resp.raise_for_status()
        log.info("Seeded shared project conventions")
    except Exception as exc:
        log.warning("Failed to seed shared conventions: %s", exc)


# ---------------------------------------------------------------------------
# Task submission
# ---------------------------------------------------------------------------


def _submit_tasks(
    plan: list[dict],
    orch_url: str,
    change_id: str,
    gateway_url: str = "http://localhost:8081",
    snapshot: str = "",
    description: str = "",
) -> list[str]:
    """Create tasks, seed identity memory for each agent type, approve roots."""
    ordered = topo_sort(plan)
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
            resp.raise_for_status()
            created = resp.json()
            title_to_id[task_def["title"]] = created["id"]
            log.info("Created %s: %r [%s]", created["id"], created["title"], created["owner"])

        # Approve root tasks (no depends_on) — dispatcher will launch them immediately.
        for task_def in ordered:
            if task_def.get("depends_on"):
                continue
            task_id = title_to_id[task_def["title"]]
            resp = client.post(
                f"/tasks/{task_id}/transition",
                json={
                    "new_status": "assigned",
                    "actor": "root-agent",
                    "payload": {"change_id": change_id},
                },
            )
            resp.raise_for_status()
            log.info("Approved %s -> assigned", task_id)

    # Seed identity memory for each unique agent type in the plan.
    seen_agents: set[str] = set()
    task_ids = list(title_to_id.values())
    sample_task_id_for_shared = task_ids[0] if task_ids else ""
    for task_def in ordered:
        agent_id = task_def["owner"]
        if agent_id in seen_agents:
            continue
        seen_agents.add(agent_id)
        sample_task_id = title_to_id[task_def["title"]]
        _seed_identity(agent_id, snapshot, description, orch_url, gateway_url, sample_task_id)

    # Seed shared project conventions for all agents.
    if sample_task_id_for_shared:
        _seed_shared_conventions(snapshot, description, gateway_url, sample_task_id_for_shared)

    return task_ids


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


class RootAgent:
    def __init__(
        self,
        repo_path: Path,
        orch_url: str,
        session_factory,
        redis_url: str | None = None,
        gateway_url: str = "http://localhost:8081",
    ) -> None:
        self._repo = repo_path
        self._orch_url = orch_url
        self._gateway_url = gateway_url
        self._consumer = StreamConsumer(
            consumer_group="root-agent",
            consumer_name="root-0",
            session_factory=session_factory,
            redis_url=redis_url,
            stream_key=ROOT_STREAM_KEY,
        )
        self._agent_type = os.getenv("AGENT_TYPE", "claude-code")
        self._tick = 0

    def start(self) -> None:
        self._consumer.ensure_group()
        log.info(
            "Root agent started on stream %s (agent_type=%s)", ROOT_STREAM_KEY, self._agent_type
        )
        while True:
            self._consumer.reclaim_pending()
            self._consumer.consume_one(self._handle)
            self._tick += 1

    def _handle(self, fields: dict) -> None:
        event_type = fields.get("event_type", "")
        if event_type != "CHANGE_REQUEST":
            log.debug("Ignoring event_type=%s", event_type)
            return

        try:
            payload = json.loads(fields.get("payload", "{}"))
        except json.JSONDecodeError:
            log.warning("Bad payload JSON in CHANGE_REQUEST")
            return

        description = payload.get("description", "").strip()
        spec_path_str = payload.get("spec_path", "").strip()
        change_id = fields.get("event_id") or str(uuid.uuid4())

        if not description:
            log.warning("CHANGE_REQUEST has empty description; skipping")
            return

        log.info("Processing change request [%s]: %s", change_id, description[:80])

        # Read optional spec file from the managed repo.
        spec_content = ""
        if spec_path_str:
            spec_file = self._repo / spec_path_str
            if spec_file.is_file():
                spec_content = spec_file.read_text(encoding="utf-8")
                log.info("Loaded spec file: %s (%d chars)", spec_path_str, len(spec_content))
            else:
                log.warning("spec_path %r not found in repo; ignoring", spec_path_str)

        # Snapshot the project.
        snapshot = _project_snapshot(self._repo)

        # Fetch existing tasks so the planner can avoid re-creating closed work.
        existing_tasks = _fetch_existing_tasks(self._orch_url)
        if existing_tasks:
            log.info(
                "Injecting %d existing task(s) into planner context", existing_tasks.count("\n")
            )

        # Decompose into tasks.
        use_api = self._agent_type == "python" and os.getenv("ANTHROPIC_API_KEY", "").strip()
        try:
            if use_api:
                raw = _decompose_with_llm(
                    description, spec_content, snapshot, existing_tasks, self._agent_type
                )
            else:
                raw = _decompose_with_claude(
                    description, spec_content, snapshot, existing_tasks, self._agent_type
                )
        except Exception as exc:
            log.error("Decomposition failed for change [%s]: %s", change_id, exc)
            return

        try:
            plan = parse_task_plan(raw)
        except (json.JSONDecodeError, IndexError, ValueError) as exc:
            log.error("Could not parse task plan for [%s]: %s\nRaw: %s", change_id, exc, raw[:500])
            return

        if not isinstance(plan, list) or not plan:
            log.error("Empty task plan for change [%s]; raw: %s", change_id, raw[:200])
            return

        log.info("Plan for [%s]: %d tasks", change_id, len(plan))

        try:
            task_ids = _submit_tasks(
                plan,
                self._orch_url,
                change_id,
                gateway_url=self._gateway_url,
                snapshot=snapshot,
                description=description,
            )
            log.info("Dispatched %d tasks for change [%s]: %s", len(task_ids), change_id, task_ids)
        except Exception as exc:
            log.error("Task submission failed for change [%s]: %s", change_id, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@app.command()
def main(
    repo: str = typer.Option(None, "--repo", "-r", help="Managed repo path."),
    orchestrator_url: str = typer.Option(None, "--orchestrator-url", help="Orchestrator base URL."),
    redis_url: str = typer.Option(None, "--redis-url", help="Redis URL."),
    gateway_url: str = typer.Option(None, "--gateway-url", help="Gateway base URL."),
) -> None:
    """Persistent root agent — consumes change requests and dispatches sub-agents."""
    repo_path = Path(repo or os.getenv("SANDBOX_REPO_PATH", "./sandbox/sample-project")).resolve()
    orch_url = orchestrator_url or os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")
    r_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6380")
    gw_url = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8081")
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra",
    )

    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    agent = RootAgent(repo_path, orch_url, factory, r_url, gateway_url=gw_url)
    try:
        agent.start()
    except KeyboardInterrupt:
        log.info("Root agent stopped.")
        sys.exit(0)


if __name__ == "__main__":
    app()
