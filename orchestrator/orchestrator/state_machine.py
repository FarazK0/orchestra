"""Task state machine.

Every status change must go through `transition()`. It atomically:
  1. Validates the (from_status, to_status) pair.
  2. Updates tasks.status and tasks.updated_at.
  3. Inserts an event row (append-only).
  4. Inserts an audit row linked to that event.

The caller owns the transaction: call session.commit() to persist,
or session.rollback() to abort. `transition()` never commits itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import AuditRow, Event, Task

# Valid (from_status, to_status) -> event_type emitted on that edge.
# This is the complete transition table; anything not here is rejected.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("created", "assigned"): "TASK_ASSIGNED",
    ("assigned", "running"): "TASK_STARTED",
    ("running", "completed"): "TASK_COMPLETED",
    ("running", "failed"): "TASK_FAILED",
    ("completed", "validated"): "TASK_VALIDATED",
    ("completed", "failed"): "TASK_FAILED",
    ("validated", "merged"): "TASK_MERGED",
    ("merged", "closed"): "TASK_CLOSED",
    ("failed", "running"): "TASK_RETRIED",
    ("failed", "escalated"): "TASK_ESCALATED",
    ("escalated", "running"): "TASK_RESET",
    ("escalated", "completed"): "TASK_RECOVER",
    # v0.3 adaptive lifecycle: parent suspends while child work runs, then resumes
    ("running", "blocked"): "TASK_BLOCKED",
    ("blocked", "assigned"): "TASK_RESUMED",
    # Cancel from any non-terminal state
    ("created", "cancelled"): "TASK_CANCELLED",
    ("assigned", "cancelled"): "TASK_CANCELLED",
    ("running", "cancelled"): "TASK_CANCELLED",
    ("blocked", "cancelled"): "TASK_CANCELLED",
    ("completed", "cancelled"): "TASK_CANCELLED",
    ("validated", "cancelled"): "TASK_CANCELLED",
    ("failed", "cancelled"): "TASK_CANCELLED",
    ("escalated", "cancelled"): "TASK_CANCELLED",
}

# All reachable statuses (used for validation)
VALID_STATUSES: frozenset[str] = frozenset(
    {s for pair in TRANSITIONS for s in pair} | {"cancelled", "closed"}
)


class InvalidTransitionError(ValueError):
    """Raised when the requested (from, to) pair is not in TRANSITIONS."""


class TaskNotFoundError(KeyError):
    """Raised when task_id does not exist in the tasks table."""


def transition(
    session: Session,
    task_id: str,
    new_status: str,
    actor: str,
    payload: dict | None = None,
    details: dict | None = None,
) -> Event:
    """Transition *task_id* to *new_status* inside the caller's transaction.

    Returns the Event row that was inserted (not yet committed).

    Args:
        session:    An open SQLAlchemy Session with an active transaction.
        task_id:    The task to transition.
        new_status: The target status (must be a valid transition destination).
        actor:      Who initiated the transition (agent_id or "human").
        payload:    Extra data stored in the event's payload JSON.
        details:    Extra data stored in the audit row's details JSON.

    Raises:
        TaskNotFoundError:      task_id not in tasks table.
        InvalidTransitionError: (current_status, new_status) not in TRANSITIONS.
    """
    now = datetime.now(timezone.utc)
    payload = dict(payload or {})
    details = dict(details or {})

    # Row-level lock prevents concurrent transitions on the same task.
    task = session.execute(
        select(Task).where(Task.id == task_id).with_for_update()
    ).scalar_one_or_none()

    if task is None:
        raise TaskNotFoundError(f"Task {task_id!r} not found")

    edge = (task.status, new_status)
    if edge not in TRANSITIONS:
        raise InvalidTransitionError(
            f"No transition from {task.status!r} to {new_status!r} for task {task_id!r}"
        )

    # Tier 2 hard gate: validated → merged requires explicit human override.
    if new_status == "merged" and task.risk_tier == 2 and not details.get("tier2_override"):
        raise InvalidTransitionError(
            f"Task {task_id!r} is Tier 2 (blocking approval). "
            "Pass details.tier2_override=True to confirm."
        )

    event_type = TRANSITIONS[edge]

    # 1. Update task status.
    task.status = new_status
    task.updated_at = now

    # 2. Append event.
    event = Event(
        event_id=uuid.uuid4(),
        schema_version=1,
        event_type=event_type,
        task_id=task_id,
        emitted_by=actor,
        emitted_at=now,
        payload={"from_status": edge[0], "to_status": new_status, **payload},
    )
    session.add(event)
    # flush so event_id is available for the foreign key in audit
    session.flush()

    # 3. Append audit row (same transaction, same DB call as the flush above).
    audit = AuditRow(
        id=uuid.uuid4(),
        timestamp=now,
        actor=actor,
        action=f"transition:{edge[0]}->{new_status}",
        task_id=task_id,
        event_id=event.event_id,
        details=details,
    )
    session.add(audit)

    return event
