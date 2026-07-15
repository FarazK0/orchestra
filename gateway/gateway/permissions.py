"""Permission checks for the tool gateway (Phase 1: allowlist on (agent_id, task_id)).

Phase 1 rule: an agent is authorised if there is a Run row with the given
(agent_id, task_id) AND the task is currently in 'running' status.

Phase 3 will replace this with signed capability-token verification at the
tool boundary. The interface here is kept narrow so that swap is localised.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import Run, Task


class PermissionDeniedError(Exception):
    pass


def check_active_run(session: Session, agent_id: str, task_id: str) -> tuple[Run, Task]:
    """Verify (agent_id, task_id) is authorised to act.

    Looks up the most recent Run for this pair and confirms the owning task
    is in 'running' status. Returns (run, task) on success.

    Raises:
        PermissionDeniedError: no matching run, or task not in 'running' state.
    """
    run = session.execute(
        select(Run)
        .where(Run.agent_id == agent_id, Run.task_id == task_id)
        .order_by(Run.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if run is None:
        raise PermissionDeniedError(f"No run found for agent {agent_id!r} on task {task_id!r}")

    task = session.get(Task, task_id)
    if task is None or task.status != "running":
        status = task.status if task else "missing"
        raise PermissionDeniedError(
            f"Task {task_id!r} is not in 'running' state (current: {status!r})"
        )

    return run, task


def check_validated_task(session: Session, task_id: str) -> Task:
    """Verify a task is in 'validated' status for the human merge gate.

    Used by POST /git/merge, which is a human-initiated action (no active run
    required). Raises PermissionDeniedError if the task is not validated.
    """
    task = session.get(Task, task_id)
    if task is None:
        raise PermissionDeniedError(f"Task {task_id!r} not found")
    if task.status != "validated":
        raise PermissionDeniedError(
            f"Merge requires 'validated' status; task {task_id!r} is {task.status!r}"
        )
    return task


def safe_path(repo_path: Path, rel_path: str) -> Path:
    """Resolve *rel_path* inside *repo_path*; raise ValueError if it escapes.

    This is the only path-safety check enforced in Phase 1. Fine-grained
    per-path scope checking (inputs/outputs lists) is deferred to Phase 3.
    """
    resolved = (repo_path / rel_path).resolve()
    if not resolved.is_relative_to(repo_path.resolve()):
        raise ValueError(f"Path {rel_path!r} escapes repo root {str(repo_path)!r}")
    return resolved
