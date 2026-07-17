"""Audit helper for the tool gateway.

Every gateway operation writes an Event row plus an AuditRow in the same
DB transaction as the side effect. This module provides that helper.

The pattern mirrors state_machine.transition(): insert Event, flush to
get the event_id FK, insert AuditRow. The caller owns the transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import AuditRow, Event

# Gateway event types, one per operation.
_EVENT_TYPES: dict[str, str] = {
    "read_artifact": "GATEWAY_READ_ARTIFACT",
    "write_artifact": "GATEWAY_WRITE_ARTIFACT",
    "run_command": "GATEWAY_RUN_COMMAND",
    "emit_event": "GATEWAY_EMIT_EVENT",
    "git_branch": "GATEWAY_GIT_BRANCH",
    "git_commit": "GATEWAY_GIT_COMMIT",
    "git_merge": "GATEWAY_GIT_MERGE",
    "memory_upsert": "GATEWAY_MEMORY_UPSERT",
}


def write_gateway_audit(
    session: Session,
    actor: str,
    operation: str,
    task_id: str,
    details: dict,
) -> Event:
    """Insert an Event + AuditRow for a gateway operation.

    Args:
        session:   Open SQLAlchemy Session. Caller must commit.
        actor:     agent_id of the caller.
        operation: One of the keys in _EVENT_TYPES (e.g. 'read_artifact').
        task_id:   Task the run belongs to.
        details:   Operation-specific metadata stored in both rows.

    Returns:
        The Event row (not yet committed).
    """
    now = datetime.now(timezone.utc)
    event_type = _EVENT_TYPES.get(operation, f"GATEWAY_{operation.upper()}")

    event = Event(
        event_id=uuid.uuid4(),
        schema_version=1,
        event_type=event_type,
        task_id=task_id,
        emitted_by=actor,
        emitted_at=now,
        payload=details,
    )
    session.add(event)
    session.flush()  # materialise event_id for the FK below

    audit = AuditRow(
        id=uuid.uuid4(),
        timestamp=now,
        actor=actor,
        action=f"gateway:{operation}",
        task_id=task_id,
        event_id=event.event_id,
        details=details,
    )
    session.add(audit)
    session.flush()

    return event
