"""Tests for orchestrator.scheduler.Scheduler.

All tests use the rolled-back session fixture from conftest.py — nothing
persists between tests. The StreamPublisher is not needed since Scheduler
no longer publishes (the Dispatcher does); these tests verify pure DB mutations.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import AuditRow, Event, Task
from orchestrator.orchestrator.scheduler import MAX_SPAWN_DEPTH, Scheduler
from orchestrator.tests.conftest import make_task

_DEFAULT_BUDGET = {"tokens": 100_000, "wall_clock_min": 30, "retries": 2}


def _make_discovery_event(
    session: Session,
    parent_task_id: str,
    title: str = "Run migration",
    owner_hint: str = "backend-agent",
    outputs: list[str] | None = None,
    dependencies: list[str] | None = None,
    checkpoint: dict | None = None,
) -> Event:
    """Insert a TASK_DISCOVERED Event row and return it."""
    ev = Event(
        event_id=__import__("uuid").uuid4(),
        schema_version=1,
        event_type="TASK_DISCOVERED",
        task_id=parent_task_id,
        emitted_by="backend-agent",
        emitted_at=datetime.now(timezone.utc),
        payload={
            "parent_task_id": parent_task_id,
            "title": title,
            "owner_hint": owner_hint,
            "reason": "test reason",
            "outputs": outputs or ["db/migration.sql"],
            "dependencies": dependencies or [],
            "checkpoint": checkpoint
            or {
                "summary": "done step 1",
                "completed_steps": ["step 1"],
                "next_step": "step 2",
            },
        },
    )
    session.add(ev)
    session.flush()
    return ev


# ---------------------------------------------------------------------------
# Happy path: discovery accepted
# ---------------------------------------------------------------------------


def test_discovery_creates_child_and_blocks_parent(session):
    parent = make_task(session, "TASK-S01", status="running")
    ev = _make_discovery_event(session, "TASK-S01")

    scheduler = Scheduler()
    child = scheduler.handle_task_discovered(session, ev)

    assert child is not None
    assert child.parent_task_id == "TASK-S01"
    assert child.spawn_depth == 1
    assert child.status == "created"
    assert child.owner == "backend-agent"
    assert "db/migration.sql" in child.outputs

    # Parent was blocked
    session.expire(parent)
    parent = session.get(Task, "TASK-S01")
    assert parent.status == "blocked"
    assert child.id in parent.blocked_by
    assert parent.checkpoint is not None

    # TASK_BLOCKED event written
    events = session.query(Event).filter(Event.task_id == "TASK-S01").all()
    event_types = [e.event_type for e in events]
    assert "TASK_BLOCKED" in event_types
    assert "TASK_DISCOVERED" in event_types


def test_discovery_writes_audit_for_block(session):
    make_task(session, "TASK-S02", status="running")
    ev = _make_discovery_event(session, "TASK-S02")

    scheduler = Scheduler()
    scheduler.handle_task_discovered(session, ev)

    audits = session.query(AuditRow).filter(AuditRow.task_id == "TASK-S02").all()
    actions = [a.action for a in audits]
    assert any("blocked" in a for a in actions)


# ---------------------------------------------------------------------------
# Rejection: max depth exceeded
# ---------------------------------------------------------------------------


def test_discovery_rejected_at_max_depth(session):
    now = datetime.now(timezone.utc)
    deep_task = Task(
        id="TASK-SD1",
        schema_version=1,
        title="Deep task",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        spawn_depth=MAX_SPAWN_DEPTH,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(deep_task)
    session.flush()

    ev = _make_discovery_event(session, "TASK-SD1")
    scheduler = Scheduler()
    result = scheduler.handle_task_discovered(session, ev)

    assert result is None

    # Parent stays running
    session.expire(deep_task)
    deep_task = session.get(Task, "TASK-SD1")
    assert deep_task.status == "running"

    # Rejection event written
    rejection = (
        session.query(Event)
        .filter(Event.task_id == "TASK-SD1", Event.event_type == "TASK_DISCOVERY_REJECTED")
        .first()
    )
    assert rejection is not None
    assert rejection.payload["reason"] == "max_spawn_depth_exceeded"


# ---------------------------------------------------------------------------
# Rejection: duplicate task
# ---------------------------------------------------------------------------


def test_discovery_rejected_on_duplicate(session):
    now = datetime.now(timezone.utc)
    # Parent still running, but already has a non-cancelled child with same title
    make_task(session, "TASK-S03", status="running")
    existing_child = Task(
        id="TASK-S03C",
        schema_version=1,
        title="Run migration",
        owner="backend-agent",
        status="created",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-S03",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(existing_child)
    session.flush()

    ev = _make_discovery_event(session, "TASK-S03", title="Run migration")
    scheduler = Scheduler()
    result = scheduler.handle_task_discovered(session, ev)

    assert result is None

    rejection = (
        session.query(Event)
        .filter(Event.task_id == "TASK-S03", Event.event_type == "TASK_DISCOVERY_REJECTED")
        .first()
    )
    assert rejection is not None
    assert rejection.payload["reason"] == "duplicate_task"


# ---------------------------------------------------------------------------
# Rejection: circular dependency
# ---------------------------------------------------------------------------


def test_discovery_rejected_on_circular_dependency(session):
    make_task(session, "TASK-S04", status="running")
    # Child depends on its own parent → circular
    ev = _make_discovery_event(session, "TASK-S04", dependencies=["TASK-S04"])
    scheduler = Scheduler()
    result = scheduler.handle_task_discovered(session, ev)
    assert result is None

    rejection = (
        session.query(Event)
        .filter(Event.task_id == "TASK-S04", Event.event_type == "TASK_DISCOVERY_REJECTED")
        .first()
    )
    assert rejection is not None
    assert rejection.payload["reason"] == "circular_dependency"


# ---------------------------------------------------------------------------
# Rejection: unknown dependency
# ---------------------------------------------------------------------------


def test_discovery_rejected_on_unknown_dependency(session):
    make_task(session, "TASK-S05", status="running")
    ev = _make_discovery_event(session, "TASK-S05", dependencies=["TASK-GHOST"])
    scheduler = Scheduler()
    result = scheduler.handle_task_discovered(session, ev)
    assert result is None

    rejection = (
        session.query(Event)
        .filter(Event.task_id == "TASK-S05", Event.event_type == "TASK_DISCOVERY_REJECTED")
        .first()
    )
    assert rejection is not None
    assert rejection.payload["reason"] == "unknown_dependency"


# ---------------------------------------------------------------------------
# Rejection: outputs outside parent write_scope
# ---------------------------------------------------------------------------


def test_discovery_rejected_when_outputs_outside_parent_scope(session):
    now = datetime.now(timezone.utc)
    parent = Task(
        id="TASK-SS1",
        schema_version=1,
        title="Scoped parent",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=["app/"],   # parent can only write to app/
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        spawn_depth=0,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    session.flush()

    # Child claims to write to 'outside/' which is not within 'app/'
    ev = _make_discovery_event(
        session, "TASK-SS1", title="Write outside scope", outputs=["outside/secret.py"]
    )
    scheduler = Scheduler()
    result = scheduler.handle_task_discovered(session, ev)

    assert result is None

    rejection = (
        session.query(Event)
        .filter(Event.task_id == "TASK-SS1", Event.event_type == "TASK_DISCOVERY_REJECTED")
        .first()
    )
    assert rejection is not None
    assert rejection.payload["reason"] == "outputs_outside_parent_scope"

    # Parent stays running
    session.expire(parent)
    parent = session.get(Task, "TASK-SS1")
    assert parent.status == "running"


# ---------------------------------------------------------------------------
# on_child_terminal: single child → parent resumed
# ---------------------------------------------------------------------------


def test_on_child_terminal_resumes_parent(session):
    now = datetime.now(timezone.utc)
    parent = Task(
        id="TASK-SR1",
        schema_version=1,
        title="Parent",
        owner="backend-agent",
        status="blocked",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        blocked_by=["TASK-SR2"],
        checkpoint={"summary": "done half", "completed_steps": [], "next_step": "finish"},
        created_at=now,
        updated_at=now,
    )
    child = Task(
        id="TASK-SR2",
        schema_version=1,
        title="Child",
        owner="backend-agent",
        status="completed",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-SR1",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add_all([parent, child])
    session.flush()

    scheduler = Scheduler()
    resumed = scheduler.on_child_terminal(session, "TASK-SR2")

    assert len(resumed) == 1
    assert resumed[0].id == "TASK-SR1"

    session.expire(parent)
    parent = session.get(Task, "TASK-SR1")
    assert parent.status == "assigned"
    assert parent.blocked_by == []

    events = session.query(Event).filter(Event.task_id == "TASK-SR1").all()
    event_types = [e.event_type for e in events]
    assert "TASK_RESUMED" in event_types


# ---------------------------------------------------------------------------
# on_child_terminal: two children, first completes → parent still blocked
# ---------------------------------------------------------------------------


def test_on_child_terminal_partial_unblock(session):
    now = datetime.now(timezone.utc)
    parent = Task(
        id="TASK-SP1",
        schema_version=1,
        title="Parent",
        owner="backend-agent",
        status="blocked",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        blocked_by=["TASK-SP2", "TASK-SP3"],
        created_at=now,
        updated_at=now,
    )
    child_a = Task(
        id="TASK-SP2",
        schema_version=1,
        title="Child A",
        owner="backend-agent",
        status="completed",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-SP1",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    child_b = Task(
        id="TASK-SP3",
        schema_version=1,
        title="Child B",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-SP1",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add_all([parent, child_a, child_b])
    session.flush()

    scheduler = Scheduler()
    resumed = scheduler.on_child_terminal(session, "TASK-SP2")

    assert resumed == []

    session.expire(parent)
    parent = session.get(Task, "TASK-SP1")
    assert parent.status == "blocked"
    assert "TASK-SP3" in parent.blocked_by
    assert "TASK-SP2" not in parent.blocked_by


# ---------------------------------------------------------------------------
# on_child_terminal: both children complete → parent resumed
# ---------------------------------------------------------------------------


def test_on_child_terminal_both_complete(session):
    now = datetime.now(timezone.utc)
    parent = Task(
        id="TASK-SB1",
        schema_version=1,
        title="Parent",
        owner="backend-agent",
        status="blocked",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        blocked_by=["TASK-SB2", "TASK-SB3"],
        created_at=now,
        updated_at=now,
    )
    child_a = Task(
        id="TASK-SB2",
        schema_version=1,
        title="Child A",
        owner="backend-agent",
        status="completed",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-SB1",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    child_b = Task(
        id="TASK-SB3",
        schema_version=1,
        title="Child B",
        owner="backend-agent",
        status="completed",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-SB1",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add_all([parent, child_a, child_b])
    session.flush()

    scheduler = Scheduler()
    # First child completes — still blocked
    resumed = scheduler.on_child_terminal(session, "TASK-SB2")
    assert resumed == []

    # Second child completes — now resumed
    resumed = scheduler.on_child_terminal(session, "TASK-SB3")
    assert len(resumed) == 1
    assert resumed[0].id == "TASK-SB1"

    session.expire(parent)
    parent = session.get(Task, "TASK-SB1")
    assert parent.status == "assigned"
    assert parent.blocked_by == []


# ---------------------------------------------------------------------------
# Edge case: parent not blocked — on_child_terminal is a no-op
# ---------------------------------------------------------------------------


def test_on_child_terminal_no_op_if_parent_not_blocked(session):
    now = datetime.now(timezone.utc)
    parent = Task(
        id="TASK-SN1",
        schema_version=1,
        title="Parent (running)",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    child = Task(
        id="TASK-SN2",
        schema_version=1,
        title="Child",
        owner="backend-agent",
        status="completed",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-SN1",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add_all([parent, child])
    session.flush()

    scheduler = Scheduler()
    resumed = scheduler.on_child_terminal(session, "TASK-SN2")
    assert resumed == []

    session.expire(parent)
    parent = session.get(Task, "TASK-SN1")
    assert parent.status == "running"
