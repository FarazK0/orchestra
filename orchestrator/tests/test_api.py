"""Integration tests for the orchestrator FastAPI app.

Uses FastAPI's TestClient against a real Postgres test database.
The DB dependency is overridden so each test shares the rolled-back
session fixture from conftest.py — nothing persists between tests.

Requires the Docker Compose stack (`make up`) to be running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orchestrator.orchestrator.api import app, get_session
from orchestrator.orchestrator.db import Task as TaskORM
from orchestrator.tests.conftest import make_task


# ---------------------------------------------------------------------------
# Fixture: TestClient wired to the per-test rolled-back session
# ---------------------------------------------------------------------------


@pytest.fixture
def client(session: Session):
    """Return a TestClient whose DB calls use the test session (rolled back on teardown)."""

    def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /tasks
# ---------------------------------------------------------------------------


def test_create_task_minimal(client, session):
    resp = client.post("/tasks", json={"title": "Add /health endpoint"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("TASK-")
    assert body["status"] == "created"
    assert body["schema_version"] == 1
    assert body["title"] == "Add /health endpoint"
    assert body["owner"] == "human"

    # Confirm the row is visible in the shared session
    task = session.get(TaskORM, body["id"])
    assert task is not None
    assert task.status == "created"


def test_create_task_with_options(client):
    resp = client.post(
        "/tasks",
        json={
            "title": "Implement auth",
            "owner": "backend-agent",
            "risk_tier": 2,
            "acceptance": ["POST /login returns 200", "tests pass"],
            "budget": {"tokens": 50_000, "wall_clock_min": 20, "retries": 1},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["owner"] == "backend-agent"
    assert body["risk_tier"] == 2
    assert body["acceptance"] == ["POST /login returns 200", "tests pass"]
    assert body["budget"]["retries"] == 1


def test_create_task_ids_increment(client):
    ids = []
    for i in range(3):
        resp = client.post("/tasks", json={"title": f"Task {i}"})
        assert resp.status_code == 201
        ids.append(resp.json()["id"])
    # All IDs are distinct and match TASK-NNN pattern
    assert len(set(ids)) == 3
    for tid in ids:
        assert tid.startswith("TASK-")


def test_create_task_invalid_risk_tier(client):
    resp = client.post("/tasks", json={"title": "Bad tier", "risk_tier": 5})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /tasks
# ---------------------------------------------------------------------------


def test_list_tasks_empty(client):
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_tasks_returns_all(client, session):
    make_task(session, "TASK-101", status="created")
    make_task(session, "TASK-102", status="running")
    session.flush()

    resp = client.get("/tasks")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert "TASK-101" in ids
    assert "TASK-102" in ids


def test_list_tasks_status_filter(client, session):
    make_task(session, "TASK-201", status="created")
    make_task(session, "TASK-202", status="running")
    make_task(session, "TASK-203", status="failed")
    session.flush()

    resp = client.get("/tasks", params={"status": "running"})
    assert resp.status_code == 200
    statuses = {t["status"] for t in resp.json()}
    assert statuses == {"running"}


def test_list_tasks_multi_status_filter(client, session):
    make_task(session, "TASK-301", status="created")
    make_task(session, "TASK-302", status="running")
    make_task(session, "TASK-303", status="closed")
    session.flush()

    resp = client.get("/tasks", params=[("status", "created"), ("status", "running")])
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert "TASK-301" in ids
    assert "TASK-302" in ids
    assert "TASK-303" not in ids


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}
# ---------------------------------------------------------------------------


def test_get_task(client, session):
    make_task(session, "TASK-401", title="Get me", status="assigned")
    session.flush()

    resp = client.get("/tasks/TASK-401")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "TASK-401"
    assert body["title"] == "Get me"
    assert body["status"] == "assigned"


def test_get_task_not_found(client):
    resp = client.get("/tasks/TASK-999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tasks/{task_id}/transition
# ---------------------------------------------------------------------------


def test_transition_valid(client, session):
    make_task(session, "TASK-501", status="created")
    session.flush()

    resp = client.post(
        "/tasks/TASK-501/transition",
        json={"new_status": "assigned", "actor": "human"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "assigned"

    # ORM state is updated within the same session
    session.expire_all()
    task = session.get(TaskORM, "TASK-501")
    assert task.status == "assigned"


def test_transition_writes_event(client, session):
    from orchestrator.orchestrator.db import Event as EventORM

    make_task(session, "TASK-502", status="created")
    session.flush()

    client.post(
        "/tasks/TASK-502/transition",
        json={"new_status": "assigned", "actor": "human"},
    )
    session.flush()

    events = session.query(EventORM).filter(EventORM.task_id == "TASK-502").all()
    assert len(events) == 1
    assert events[0].event_type == "TASK_ASSIGNED"
    assert events[0].emitted_by == "human"


def test_transition_writes_audit(client, session):
    from orchestrator.orchestrator.db import AuditRow

    make_task(session, "TASK-503", status="created")
    session.flush()

    client.post(
        "/tasks/TASK-503/transition",
        json={"new_status": "assigned", "actor": "human"},
    )
    session.flush()

    audits = session.query(AuditRow).filter(AuditRow.task_id == "TASK-503").all()
    assert len(audits) == 1
    assert audits[0].actor == "human"
    assert "created" in audits[0].action
    assert "assigned" in audits[0].action


def test_transition_invalid_returns_409(client, session):
    make_task(session, "TASK-504", status="created")
    session.flush()

    resp = client.post(
        "/tasks/TASK-504/transition",
        json={"new_status": "merged", "actor": "human"},  # invalid jump
    )
    assert resp.status_code == 409


def test_transition_task_not_found_returns_404(client):
    resp = client.post(
        "/tasks/TASK-999/transition",
        json={"new_status": "assigned", "actor": "human"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}/events
# ---------------------------------------------------------------------------


def test_list_task_events_empty(client, session):
    make_task(session, "TASK-601", status="created")
    session.flush()

    resp = client.get("/tasks/TASK-601/events")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_task_events_after_transition(client, session):
    make_task(session, "TASK-602", status="created")
    session.flush()

    client.post(
        "/tasks/TASK-602/transition",
        json={"new_status": "assigned", "actor": "human"},
    )
    session.flush()

    resp = client.get("/tasks/TASK-602/events")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    assert events[0]["event_type"] == "TASK_ASSIGNED"
    assert events[0]["task_id"] == "TASK-602"


def test_list_task_events_not_found(client):
    resp = client.get("/tasks/TASK-999/events")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tasks/{task_id}/run
# ---------------------------------------------------------------------------


def test_start_run_creates_run_and_transitions(client, session, tmp_path):
    from orchestrator.orchestrator.db import Run

    make_task(session, "TASK-701", status="assigned")
    session.flush()

    resp = client.post(
        "/tasks/TASK-701/run",
        json={"agent_id": "backend-agent", "repo_path": str(tmp_path)},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["task_id"] == "TASK-701"
    assert body["agent_id"] == "backend-agent"
    assert body["branch"] == "agent/backend/TASK-701"
    assert "run_id" in body
    assert body["context_package_ref"].endswith(".json")

    # Task transitioned to running
    session.expire_all()
    task = session.get(TaskORM, "TASK-701")
    assert task.status == "running"

    # Run row exists
    from uuid import UUID

    run = session.get(Run, UUID(body["run_id"]))
    assert run is not None
    assert run.task_id == "TASK-701"


def test_start_run_writes_context_package_file(client, session, tmp_path):
    import json

    make_task(session, "TASK-702", status="assigned")
    session.flush()

    store = tmp_path / "ctx"
    resp = client.post(
        "/tasks/TASK-702/run",
        json={
            "agent_id": "backend-agent",
            "repo_path": str(tmp_path),
            "store_dir": str(store),
        },
    )
    assert resp.status_code == 201
    ref = resp.json()["context_package_ref"]

    from pathlib import Path

    pkg_path = Path(ref)
    assert pkg_path.exists()
    pkg = json.loads(pkg_path.read_text())
    assert pkg["task_id"] == "TASK-702"
    assert pkg["schema_version"] == 1


def test_start_run_writes_transition_event(client, session, tmp_path):
    from orchestrator.orchestrator.db import Event as EventORM

    make_task(session, "TASK-703", status="assigned")
    session.flush()

    client.post(
        "/tasks/TASK-703/run",
        json={"agent_id": "backend-agent", "repo_path": str(tmp_path)},
    )
    session.flush()

    events = session.query(EventORM).filter(EventORM.task_id == "TASK-703").all()
    types = {e.event_type for e in events}
    assert "TASK_STARTED" in types


def test_start_run_rejects_non_assigned_task(client, session, tmp_path):
    make_task(session, "TASK-704", status="created")
    session.flush()

    resp = client.post(
        "/tasks/TASK-704/run",
        json={"agent_id": "backend-agent", "repo_path": str(tmp_path)},
    )
    assert resp.status_code == 409


def test_start_run_task_not_found(client, tmp_path):
    resp = client.post(
        "/tasks/TASK-999/run",
        json={"agent_id": "backend-agent", "repo_path": str(tmp_path)},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Policy-based tier assignment
# ---------------------------------------------------------------------------


def test_create_task_tier_auto_assigned_from_policy(client):
    """Output path matching infra/migrations/** → risk_tier raised to 2 by policy."""
    resp = client.post(
        "/tasks",
        json={
            "title": "Add migration",
            "outputs": ["infra/migrations/008_new_column.py"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["risk_tier"] == 2


def test_create_task_explicit_tier_not_lowered_by_policy(client):
    """Explicit risk_tier=2 is preserved even if policy would assign tier 0."""
    resp = client.post(
        "/tasks",
        json={
            "title": "Sensitive docs task",
            "risk_tier": 2,
            "outputs": ["docs/adr/ADR-010.md"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["risk_tier"] == 2


def test_create_task_docs_output_gets_tier0(client):
    """docs/** matches the tier-0 rule; default tier 1 is lowered to 0 by policy."""
    resp = client.post(
        "/tasks",
        json={
            "title": "Update docs",
            "outputs": ["docs/design/spec.md"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["risk_tier"] == 0


# ---------------------------------------------------------------------------
# Tier 2 hard gate (state machine)
# ---------------------------------------------------------------------------


def test_tier2_transition_blocked_without_override(client, session):
    """validated → merged on a Tier 2 task is rejected without tier2_override."""
    task = make_task(session, "TASK-801", status="validated")
    task.risk_tier = 2
    session.flush()

    resp = client.post(
        "/tasks/TASK-801/transition",
        json={"new_status": "merged", "actor": "human"},
    )
    assert resp.status_code == 409
    assert "Tier 2" in resp.json()["detail"]


def test_tier2_transition_allowed_with_override(client, session):
    """validated → merged on a Tier 2 task succeeds when tier2_override=True."""
    task = make_task(session, "TASK-802", status="validated")
    task.risk_tier = 2
    session.flush()

    resp = client.post(
        "/tasks/TASK-802/transition",
        json={
            "new_status": "merged",
            "actor": "human",
            "details": {"tier2_override": True},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"


def test_tier1_transition_not_blocked(client, session):
    """validated → merged on a Tier 1 task succeeds without any override."""
    make_task(session, "TASK-803", status="validated")
    session.flush()

    resp = client.post(
        "/tasks/TASK-803/transition",
        json={"new_status": "merged", "actor": "human"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"
