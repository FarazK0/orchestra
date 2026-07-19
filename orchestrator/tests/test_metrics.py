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
