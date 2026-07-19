"""Permission checks for the tool gateway.

Two layers of enforcement (belt-and-suspenders):
  1. Capability token (Phase 3): signed HS256 JWT minted by the orchestrator,
     verifies the caller is the legitimate agent for this run. Only active when
     CAPABILITY_SECRET is set; falls back to DB-only auth otherwise.
  2. DB active-run check (Phase 1): confirms a Run row exists for
     (agent_id, task_id) and the task is in 'running' status.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import Run, Task


class PermissionDeniedError(Exception):
    pass


def verify_capability_header(authorization: str | None) -> dict:
    """Verify the Authorization: Bearer <token> header and return its claims.

    If CAPABILITY_SECRET is not set, skips verification and returns an empty
    dict (backwards-compatible mode — DB check still runs).

    Raises:
        PermissionDeniedError: header is absent/malformed or token is invalid
            when CAPABILITY_SECRET is configured.
    """
    if not os.getenv("CAPABILITY_SECRET", ""):
        return {}

    if not authorization or not authorization.startswith("Bearer "):
        raise PermissionDeniedError("Missing capability token")

    token_str = authorization[len("Bearer ") :]
    try:
        from .token import verify_token

        return verify_token(token_str)
    except Exception as exc:
        raise PermissionDeniedError(f"Invalid capability token: {exc}") from exc


def check_write_scope(claims: dict, path: str) -> None:
    """Verify *path* is within the token's declared write scope.

    No-op if claims is empty (no token configured) or write_scope is empty
    (unrestricted).

    Raises:
        PermissionDeniedError: path is outside the declared scope entries.
    """
    write_scope: list[str] = claims.get("write_scope", [])
    if not write_scope:
        return

    for prefix in write_scope:
        normalised = prefix.rstrip("/")
        if path == normalised or path.startswith(normalised + "/"):
            return

    raise PermissionDeniedError(f"Path {path!r} is outside write scope {write_scope}")


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
