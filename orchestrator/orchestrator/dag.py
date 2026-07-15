"""DAG readiness and concurrency-conflict detection for the Orchestra scheduler.

Pure functions over the Postgres control plane — no side effects.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Task

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "validated", "merged", "closed"})


def task_is_ready(task: Task, session: Session) -> bool:
    """True when all of task's depends_on are in a terminal status (or there are none)."""
    if not task.depends_on:
        return True
    rows = session.execute(select(Task).where(Task.id.in_(task.depends_on))).scalars().all()
    return all(t.status in TERMINAL_STATUSES for t in rows)


def get_ready_successors(completed_task_id: str, session: Session) -> list[Task]:
    """Tasks in 'created' state that are fully unblocked when completed_task_id finishes."""
    candidates = session.execute(select(Task).where(Task.status == "created")).scalars().all()
    return [
        t
        for t in candidates
        if completed_task_id in (t.depends_on or []) and task_is_ready(t, session)
    ]


def outputs_overlap(paths_a: list[str], paths_b: list[str]) -> bool:
    """True if any path in paths_a is a prefix of (or equal to) a path in paths_b, or vice versa."""
    for a in paths_a:
        a_dir = a.rstrip("/") + "/"
        for b in paths_b:
            b_dir = b.rstrip("/") + "/"
            if a == b or b.startswith(a_dir) or a.startswith(b_dir):
                return True
    return False


def get_running_conflicts(task: Task, session: Session) -> list[Task]:
    """Running tasks whose outputs overlap with this task's outputs."""
    if not task.outputs:
        return []
    running = session.execute(select(Task).where(Task.status == "running")).scalars().all()
    return [
        t for t in running if t.id != task.id and outputs_overlap(task.outputs, t.outputs or [])
    ]
