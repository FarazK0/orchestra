"""Context packager: assembles and persists the context package for an agent run.

For each task the packager:
1. Reads the task spec and acceptance criteria from Postgres.
2. Reads the content of every file listed in task.inputs from the managed repo.
3. Reads all ADRs from docs/adr/ in the managed repo.
4. Serialises the package to {store_dir}/{run_id}.json on disk.
5. Inserts a Run row in Postgres pointing to that file.

The context package is both the agent's read scope and its briefing document --
the exact same dict is stored on disk and handed to the agent, so a run is
fully reproducible from its context_package_ref alone.

Callers own the transaction; this module never commits.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .db import Run, Task


class TaskNotFoundError(Exception):
    pass


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return None


def build_context_package(
    session: Session,
    task_id: str,
    repo_path: Path,
) -> dict:
    """Assemble the context package dict for *task_id*.

    Reads task spec from Postgres and artifact contents from *repo_path*
    on disk. Returns a plain dict; does not touch the DB or the filesystem.

    Args:
        session:   An open SQLAlchemy Session.
        task_id:   The task to package.
        repo_path: Absolute path to the root of the managed Git repo.

    Raises:
        TaskNotFoundError: task_id is not in the tasks table.
    """
    task = session.get(Task, task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id!r} not found")

    # Input artifacts listed in the task spec
    input_artifacts: list[dict] = []
    for rel_path in task.inputs:
        content = _read_file(repo_path / rel_path)
        input_artifacts.append({"path": rel_path, "content": content, "found": content is not None})

    # ADRs from docs/adr/ -- decision memory for the agent
    adr_dir = repo_path / "docs" / "adr"
    adrs: list[dict] = []
    if adr_dir.is_dir():
        for adr_file in sorted(adr_dir.glob("*.md")):
            content = _read_file(adr_file)
            if content is not None:
                adrs.append({"path": str(adr_file.relative_to(repo_path)), "content": content})

    branch = f"agent/backend/{task_id}"

    return {
        "schema_version": 1,
        "task_id": task_id,
        "packaged_at": datetime.now(timezone.utc).isoformat(),
        "task": {
            "id": task.id,
            "title": task.title,
            "owner": task.owner,
            "status": task.status,
            "depends_on": task.depends_on,
            "inputs": task.inputs,
            "outputs": task.outputs,
            "acceptance": task.acceptance,
            "risk_tier": task.risk_tier,
            "budget": task.budget,
        },
        "input_artifacts": input_artifacts,
        "adrs": adrs,
        "agent_instructions": {
            "branch": branch,
            "commit_prefix": f"[{task_id}]",
            "read_scope": task.inputs,
            "write_scope": task.outputs,
            "acceptance_criteria": task.acceptance,
        },
    }


def create_run(
    session: Session,
    task_id: str,
    agent_id: str,
    repo_path: Path,
    store_dir: Path,
) -> Run:
    """Build and persist a context package; insert and return the new Run row.

    Writes the package JSON to {store_dir}/{run_id}.json, then adds a Run
    row to the session. The caller must commit the transaction.

    Args:
        session:   An open SQLAlchemy Session.
        task_id:   Task being run.
        agent_id:  Identity of the agent that will consume this run.
        repo_path: Root of the managed Git repo (for reading artifacts).
        store_dir: Directory where the context package JSON is written.

    Raises:
        TaskNotFoundError: task_id is not in the tasks table.
    """
    run_id = uuid.uuid4()
    package = build_context_package(session, task_id, repo_path)
    package["run_id"] = str(run_id)

    store_dir.mkdir(parents=True, exist_ok=True)
    package_path = store_dir / f"{run_id}.json"
    package_path.write_text(json.dumps(package, indent=2, default=str), encoding="utf-8")

    branch = package["agent_instructions"]["branch"]
    now = datetime.now(timezone.utc)

    run = Run(
        run_id=run_id,
        schema_version=1,
        task_id=task_id,
        agent_id=agent_id,
        branch=branch,
        context_package_ref=str(package_path),
        started_at=now,
    )
    session.add(run)
    session.flush()
    return run
