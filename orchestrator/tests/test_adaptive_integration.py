"""End-to-end integration test for the v0.3 adaptive lifecycle.

Exercises the full path — discovery, block, child completion, resume, context package
verification — inside a single rolled-back DB transaction.  No Redis or subprocess
is needed; the Scheduler is called directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from orchestrator.orchestrator.context_packager import build_context_package
from orchestrator.orchestrator.db import Event, Task
from orchestrator.orchestrator.scheduler import MAX_BLOCKED_BY, Scheduler
from orchestrator.orchestrator.state_machine import transition
from orchestrator.tests.conftest import make_task

_DEFAULT_BUDGET = {"tokens": 100_000, "wall_clock_min": 30, "retries": 2}


def _make_discovery_event(
    session,
    parent_task_id: str,
    title: str = "Run DB migration",
    checkpoint: dict | None = None,
    deps: list[str] | None = None,
) -> Event:
    ev = Event(
        event_id=uuid.uuid4(),
        schema_version=1,
        event_type="TASK_DISCOVERED",
        task_id=parent_task_id,
        emitted_by="backend-agent",
        emitted_at=datetime.now(timezone.utc),
        payload={
            "parent_task_id": parent_task_id,
            "title": title,
            "owner_hint": "backend-agent",
            "reason": "migration must run before schema use",
            "outputs": ["db/migration.sql"],
            "dependencies": deps or [],
            "checkpoint": checkpoint
            or {
                "summary": "auth endpoints done",
                "completed_steps": ["auth"],
                "next_step": "use migration result",
            },
        },
    )
    session.add(ev)
    session.flush()
    return ev


# ---------------------------------------------------------------------------
# Full lifecycle: discovery → block → child completes → resume → context
# ---------------------------------------------------------------------------


def test_full_adaptive_lifecycle(session, tmp_path):
    checkpoint = {
        "summary": "done step 1",
        "completed_steps": ["step 1"],
        "next_step": "step 2",
    }
    parent = make_task(session, "TASK-INT1", status="running")
    ev = _make_discovery_event(session, "TASK-INT1", checkpoint=checkpoint)

    scheduler = Scheduler()

    # 1. Discovery: child created, parent blocked
    child = scheduler.handle_task_discovered(session, ev)
    assert child is not None
    assert child.status == "created"
    assert child.parent_task_id == "TASK-INT1"
    assert child.spawn_depth == 1

    # 2. Parent is now blocked
    session.expire(parent)
    parent = session.get(Task, "TASK-INT1")
    assert parent.status == "blocked"
    assert child.id in parent.blocked_by
    assert parent.checkpoint == checkpoint

    # 3. Dispatcher assigns child; child runs and completes
    for new_status in ("assigned", "running", "completed"):
        transition(session, child.id, new_status, actor="dispatcher")

    # 4. on_child_terminal → parent unblocked
    resumed = scheduler.on_child_terminal(session, child.id)
    assert len(resumed) == 1
    assert resumed[0].id == "TASK-INT1"

    session.expire(parent)
    parent = session.get(Task, "TASK-INT1")
    assert parent.status == "assigned"
    assert parent.blocked_by == []

    # 5. Resumed context package has checkpoint + child_outputs
    pkg = build_context_package(session, "TASK-INT1", tmp_path)
    assert pkg["is_resumption"] is True
    assert pkg["checkpoint"] == checkpoint
    assert len(pkg["child_outputs"]) == 1
    co = pkg["child_outputs"][0]
    assert co["task_id"] == child.id
    assert co["status"] == "completed"
    assert "db/migration.sql" in co["outputs"]

    # 6. Parent resumes: running → completed
    for new_status in ("running", "completed"):
        transition(session, "TASK-INT1", new_status, actor="backend-agent")

    # 7. Full event trail exists
    event_types = {
        e.event_type for e in session.query(Event).filter(Event.task_id == "TASK-INT1").all()
    }
    assert {"TASK_DISCOVERED", "TASK_BLOCKED", "TASK_RESUMED", "TASK_COMPLETED"} <= event_types


# ---------------------------------------------------------------------------
# MAX_BLOCKED_BY guard
# ---------------------------------------------------------------------------


def test_max_blocked_by_guard(session):
    now = datetime.now(timezone.utc)
    # Parent whose blocked_by is already full
    fake_ids = [f"TASK-F{i:02d}" for i in range(MAX_BLOCKED_BY)]
    parent = Task(
        id="TASK-MB1",
        schema_version=1,
        title="Max blocked parent",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        spawn_depth=0,
        blocked_by=fake_ids,
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    session.flush()

    ev = _make_discovery_event(session, "TASK-MB1")
    result = Scheduler().handle_task_discovered(session, ev)
    assert result is None

    rejection = (
        session.query(Event)
        .filter(Event.task_id == "TASK-MB1", Event.event_type == "TASK_DISCOVERY_REJECTED")
        .first()
    )
    assert rejection is not None
    assert rejection.payload["reason"] == "max_blocked_by_exceeded"

    # Parent stays running (was never transitioned to blocked)
    session.expire(parent)
    parent = session.get(Task, "TASK-MB1")
    assert parent.status == "running"


# ---------------------------------------------------------------------------
# Normalized title duplicate detection
# ---------------------------------------------------------------------------


def test_normalized_title_duplicate_detection(session):
    now = datetime.now(timezone.utc)
    make_task(session, "TASK-ND1", status="running")

    # Pre-existing child with whitespace and mixed case
    existing_child = Task(
        id="TASK-ND2",
        schema_version=1,
        title="  Run Migration  ",
        owner="backend-agent",
        status="created",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget=_DEFAULT_BUDGET,
        parent_task_id="TASK-ND1",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(existing_child)
    session.flush()

    # Discovery event uses lower-case, no spaces — should still be detected as duplicate
    ev = _make_discovery_event(session, "TASK-ND1", title="run migration")
    result = Scheduler().handle_task_discovered(session, ev)
    assert result is None

    rejection = (
        session.query(Event)
        .filter(Event.task_id == "TASK-ND1", Event.event_type == "TASK_DISCOVERY_REJECTED")
        .first()
    )
    assert rejection is not None
    assert rejection.payload["reason"] == "duplicate_task"
