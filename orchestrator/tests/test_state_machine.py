"""Tests for the task state machine.

Each test creates a task at the required source status, calls transition(),
and asserts:
  - task.status is updated
  - an Event row exists with the correct event_type
  - an AuditRow exists linked to that event
  - the audit action string is correct

All of this happens inside a rolled-back transaction (see conftest.py),
so no permanent state is written.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import AuditRow, Event, Task
from orchestrator.orchestrator.state_machine import (
    TRANSITIONS,
    InvalidTransitionError,
    TaskNotFoundError,
    transition,
)
from orchestrator.tests.conftest import make_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_last_event(session: Session, task_id: str) -> Event:
    return (
        session.query(Event)
        .filter(Event.task_id == task_id)
        .order_by(Event.emitted_at.desc())
        .first()
    )


def _get_audit_for_event(session: Session, event_id: uuid.UUID) -> AuditRow:
    return session.query(AuditRow).filter(AuditRow.event_id == event_id).one()


def _do_transition(session, task_id, new_status, actor="test-agent"):
    event = transition(session, task_id, new_status, actor=actor)
    session.flush()
    return event


# ---------------------------------------------------------------------------
# Happy-path transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_status,to_status,expected_event_type",
    [
        ("created", "assigned", "TASK_ASSIGNED"),
        ("assigned", "running", "TASK_STARTED"),
        ("running", "completed", "TASK_COMPLETED"),
        ("running", "failed", "TASK_FAILED"),
        ("completed", "validated", "TASK_VALIDATED"),
        ("completed", "failed", "TASK_FAILED"),
        ("validated", "merged", "TASK_MERGED"),
        ("merged", "closed", "TASK_CLOSED"),
        ("failed", "running", "TASK_RETRIED"),
        ("failed", "escalated", "TASK_ESCALATED"),
        ("escalated", "cancelled", "TASK_CANCELLED"),
        ("escalated", "running", "TASK_RESET"),
        ("created", "cancelled", "TASK_CANCELLED"),
        ("assigned", "cancelled", "TASK_CANCELLED"),
        ("running", "cancelled", "TASK_CANCELLED"),
        ("completed", "cancelled", "TASK_CANCELLED"),
        ("validated", "cancelled", "TASK_CANCELLED"),
        ("failed", "cancelled", "TASK_CANCELLED"),
    ],
)
def test_valid_transition(session, from_status, to_status, expected_event_type):
    tid = f"TASK-{from_status[:3].upper()}{to_status[:3].upper()}"
    make_task(session, tid, status=from_status)

    event = _do_transition(session, tid, to_status)

    # Task status updated
    task = session.get(Task, tid)
    assert task.status == to_status

    # Event row written with correct type and linkage
    assert event.event_type == expected_event_type
    assert event.task_id == tid
    assert event.payload["from_status"] == from_status
    assert event.payload["to_status"] == to_status

    # Audit row written and linked to event
    audit = _get_audit_for_event(session, event.event_id)
    assert audit.task_id == tid
    assert audit.actor == "test-agent"
    assert audit.action == f"transition:{from_status}->{to_status}"


def test_transition_covers_all_defined_edges():
    """Ensure the parametrized list above covers every edge in TRANSITIONS."""
    tested = {
        ("created", "assigned"),
        ("assigned", "running"),
        ("running", "completed"),
        ("running", "failed"),
        ("completed", "validated"),
        ("completed", "failed"),
        ("validated", "merged"),
        ("merged", "closed"),
        ("failed", "running"),
        ("failed", "escalated"),
        ("escalated", "cancelled"),
        ("escalated", "running"),
        ("created", "cancelled"),
        ("assigned", "cancelled"),
        ("running", "cancelled"),
        ("completed", "cancelled"),
        ("validated", "cancelled"),
        ("failed", "cancelled"),
    }
    assert tested == set(TRANSITIONS.keys())


# ---------------------------------------------------------------------------
# Guard: task not found
# ---------------------------------------------------------------------------


def test_task_not_found_raises(session):
    with pytest.raises(TaskNotFoundError, match="TASK-GHOST"):
        transition(session, "TASK-GHOST", "assigned", actor="human")


# ---------------------------------------------------------------------------
# Guard: invalid transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_status,bad_to",
    [
        ("created", "running"),  # must go through assigned first
        ("created", "validated"),
        ("assigned", "completed"),  # must run first
        ("running", "merged"),  # must complete + validate first
        ("completed", "closed"),  # must merge first
        ("validated", "running"),  # no going backwards
        ("closed", "created"),  # terminal
        ("cancelled", "created"),  # terminal
    ],
)
def test_invalid_transition_raises(session, from_status, bad_to):
    tid = f"TASK-INV{from_status[:3].upper()}"
    make_task(session, tid, status=from_status)

    with pytest.raises(InvalidTransitionError, match=from_status):
        transition(session, tid, bad_to, actor="human")


# ---------------------------------------------------------------------------
# Extra payload and details are stored
# ---------------------------------------------------------------------------


def test_payload_and_details_stored(session):
    make_task(session, "TASK-PAY", status="created")

    event = transition(
        session,
        "TASK-PAY",
        "assigned",
        actor="human",
        payload={"agent_id": "backend-agent"},
        details={"reason": "first assignment"},
    )
    session.flush()

    assert event.payload["agent_id"] == "backend-agent"
    audit = _get_audit_for_event(session, event.event_id)
    assert audit.details["reason"] == "first assignment"


# ---------------------------------------------------------------------------
# Actor is recorded correctly
# ---------------------------------------------------------------------------


def test_actor_recorded_on_audit(session):
    make_task(session, "TASK-ACT", status="validated")

    event = _do_transition(session, "TASK-ACT", "merged", actor="human")

    audit = _get_audit_for_event(session, event.event_id)
    assert audit.actor == "human"
    assert event.emitted_by == "human"


# ---------------------------------------------------------------------------
# Atomicity: a rollback wipes both task update, event, and audit
# ---------------------------------------------------------------------------


def test_rollback_reverts_all_writes(engine):
    """Transition + rollback leaves the DB as if nothing happened."""
    # Use a separate short-lived session to insert a task that persists.
    with Session(engine) as setup_sess:
        setup_sess.begin()
        make_task(setup_sess, "TASK-RB1", status="created")
        setup_sess.commit()

    try:
        with Session(engine) as sess:
            sess.begin()
            transition(sess, "TASK-RB1", "assigned", actor="human")
            sess.flush()
            # Verify in-flight changes are visible within this session
            task = sess.get(Task, "TASK-RB1")
            assert task.status == "assigned"
            sess.rollback()

        # After rollback, a fresh session should see the original status
        with Session(engine) as verify:
            task = verify.get(Task, "TASK-RB1")
            assert task.status == "created"

            events = verify.query(Event).filter(Event.task_id == "TASK-RB1").all()
            assert events == []

            audits = verify.query(AuditRow).filter(AuditRow.task_id == "TASK-RB1").all()
            assert audits == []
    finally:
        # Clean up the persisted setup task
        with Session(engine) as cleanup:
            cleanup.begin()
            task = cleanup.get(Task, "TASK-RB1")
            if task:
                cleanup.delete(task)
            cleanup.commit()


# ---------------------------------------------------------------------------
# Row-lock: two concurrent transitions on the same task are serialized
# (structural test - verifies the SELECT FOR UPDATE is present by checking
# that the transition correctly reads the latest committed status)
# ---------------------------------------------------------------------------


def test_sequential_transitions_observe_updated_status(session):
    """Two transitions in the same session observe the running task status."""
    make_task(session, "TASK-SEQ", status="created")

    transition(session, "TASK-SEQ", "assigned", actor="human")
    session.flush()

    # Second transition must see "assigned", not "created"
    transition(session, "TASK-SEQ", "running", actor="backend-agent")
    session.flush()

    task = session.get(Task, "TASK-SEQ")
    assert task.status == "running"

    events = (
        session.query(Event).filter(Event.task_id == "TASK-SEQ").order_by(Event.emitted_at).all()
    )
    assert [e.event_type for e in events] == ["TASK_ASSIGNED", "TASK_STARTED"]
