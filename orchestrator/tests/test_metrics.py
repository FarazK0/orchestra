"""Smoke tests for the /metrics endpoint and Prometheus counter instrumentation.

Requires the Docker Compose stack (make up) to be running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orchestrator.orchestrator.api import app, get_session
from orchestrator.tests.conftest import make_task


@pytest.fixture
def client(session: Session):
    def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def test_metrics_endpoint_accessible(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "orchestra_tasks_total" in resp.text


def test_metrics_endpoint_includes_http_metrics(client):
    """prometheus-fastapi-instrumentator adds http_requests_total automatically."""
    client.get("/healthz")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text


def _counter_value(metric_name: str, labels: dict | None = None) -> float:
    """Read the current value of a named Prometheus counter (or 0 if not yet emitted)."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(metric_name, labels) or 0.0


# ---------------------------------------------------------------------------
# v0.3 adaptive metrics
# ---------------------------------------------------------------------------


def test_tasks_discovered_counter_increments(session):
    """tasks_discovered_total goes up by 1 on a successful discovery."""
    import uuid
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import Event, Task
    from orchestrator.orchestrator.scheduler import Scheduler

    now = datetime.now(timezone.utc)
    task = Task(
        id="TASK-M10",
        schema_version=1,
        title="Metrics parent",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        spawn_depth=0,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()  # task must be visible before the FK-constrained event
    ev = Event(
        event_id=uuid.uuid4(),
        schema_version=1,
        event_type="TASK_DISCOVERED",
        task_id="TASK-M10",
        emitted_by="backend-agent",
        emitted_at=now,
        payload={
            "parent_task_id": "TASK-M10",
            "title": "Metrics child",
            "owner_hint": "backend-agent",
            "reason": "test",
            "outputs": [],
            "dependencies": [],
            "checkpoint": {},
        },
    )
    session.add(ev)
    session.flush()

    before = _counter_value("orchestra_tasks_discovered_total")
    Scheduler().handle_task_discovered(session, ev)
    after = _counter_value("orchestra_tasks_discovered_total")

    assert after == before + 1.0


def test_task_discovery_rejected_counter_increments(session):
    """task_discovery_rejected_total{reason=max_spawn_depth_exceeded} increments on rejection."""
    import uuid
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import Event, Task
    from orchestrator.orchestrator.scheduler import MAX_SPAWN_DEPTH, Scheduler

    now = datetime.now(timezone.utc)
    task = Task(
        id="TASK-M20",
        schema_version=1,
        title="Deep parent",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        spawn_depth=MAX_SPAWN_DEPTH,  # already at max — next discovery must be rejected
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()  # task must be visible before the FK-constrained event
    ev = Event(
        event_id=uuid.uuid4(),
        schema_version=1,
        event_type="TASK_DISCOVERED",
        task_id="TASK-M20",
        emitted_by="backend-agent",
        emitted_at=now,
        payload={
            "parent_task_id": "TASK-M20",
            "title": "Rejected child",
            "owner_hint": "backend-agent",
            "reason": "test",
            "outputs": [],
            "dependencies": [],
            "checkpoint": {},
        },
    )
    session.add(ev)
    session.flush()

    before = _counter_value(
        "orchestra_task_discovery_rejected_total",
        {"reason": "max_spawn_depth_exceeded"},
    )
    Scheduler().handle_task_discovered(session, ev)
    after = _counter_value(
        "orchestra_task_discovery_rejected_total",
        {"reason": "max_spawn_depth_exceeded"},
    )

    assert after == before + 1.0


def test_tasks_resumed_counter_increments(session):
    """tasks_resumed_total goes up by 1 when on_child_terminal unblocks a parent."""
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import Task
    from orchestrator.orchestrator.scheduler import Scheduler

    now = datetime.now(timezone.utc)
    parent = Task(
        id="TASK-M30",
        schema_version=1,
        title="Blocked parent",
        owner="backend-agent",
        status="blocked",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        blocked_by=["TASK-M31"],
        checkpoint={},
        created_at=now,
        updated_at=now,
    )
    child = Task(
        id="TASK-M31",
        schema_version=1,
        title="Completing child",
        owner="backend-agent",
        status="completed",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        parent_task_id="TASK-M30",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add_all([parent, child])
    session.flush()

    before = _counter_value("orchestra_tasks_resumed_total")
    Scheduler().on_child_terminal(session, "TASK-M31")
    after = _counter_value("orchestra_tasks_resumed_total")

    assert after == before + 1.0


def test_tasks_total_increments_on_transition(client, session: Session):
    """Transitioning a task records it in the orchestra_tasks_total counter."""
    from orchestrator.orchestrator.metrics import tasks_total

    make_task(session, "TASK-901", status="created")
    session.flush()

    # Read the counter sample directly from the Prometheus registry.
    def _read_counter(new_status: str, owner: str) -> float:
        for metric in tasks_total.collect():
            for sample in metric.samples:
                if (
                    sample.labels.get("new_status") == new_status
                    and sample.labels.get("owner") == owner
                ):
                    return sample.value
        return 0.0

    before = _read_counter("assigned", "test-agent")

    client.post(
        "/tasks/TASK-901/transition",
        json={"new_status": "assigned", "actor": "test"},
    )

    after = _read_counter("assigned", "test-agent")
    assert after == before + 1.0

    # Verify the /metrics endpoint also exposes the label.
    assert 'new_status="assigned"' in client.get("/metrics").text
