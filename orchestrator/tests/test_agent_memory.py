"""Tests for the agent memory system.

Covers:
- context_packager injects agent_memory when memories exist
- context_packager omits agent_memory on cold start
- context_packager adds _warning at 5k-char multiples
- orchestrator GET /agent-memories endpoint
- orchestrator DELETE /agent-memories/{id} endpoint + audit trail
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orchestrator.orchestrator.api import app
from orchestrator.orchestrator.context_packager import build_context_package
from orchestrator.orchestrator.db import AgentMemory, AuditRow, Event

from .conftest import make_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    session: Session,
    agent_id: str = "claude-code-agent",
    memory_type: str = "identity",
    key: str = "identity",
    content: str = "You are the generalist engineer.",
    source_task_id: str | None = None,
) -> AgentMemory:
    now = datetime.now(timezone.utc)
    mem = AgentMemory(
        id=uuid.uuid4(),
        agent_id=agent_id,
        project_id="default",
        memory_type=memory_type,
        key=key,
        content=content,
        source_task_id=source_task_id,
        created_at=now,
        updated_at=now,
    )
    session.add(mem)
    session.flush()
    return mem


# ---------------------------------------------------------------------------
# Context packager tests
# ---------------------------------------------------------------------------


def test_context_packager_no_memory_omits_key(session, tmp_path):
    """Cold-start: no memory rows → agent_memory key absent from package."""
    make_task(session, "TASK-MEM-COLD", owner="claude-code-agent")
    pkg = build_context_package(session, "TASK-MEM-COLD", tmp_path)
    assert "agent_memory" not in pkg


def test_context_packager_injects_identity(session, tmp_path):
    """Identity memory is included in the package."""
    make_task(session, "TASK-MEM-ID", owner="claude-code-agent")
    _make_memory(
        session,
        agent_id="claude-code-agent",
        memory_type="identity",
        key="identity",
        content="You are the generalist engineer.",
    )
    pkg = build_context_package(session, "TASK-MEM-ID", tmp_path)
    assert "agent_memory" in pkg
    assert pkg["agent_memory"]["identity"] == "You are the generalist engineer."
    assert pkg["agent_memory"]["episodes"] == []
    assert pkg["agent_memory"]["skills"] == []


def test_context_packager_injects_episodes_and_skills(session, tmp_path):
    """Episode and skill memories are collected into lists."""
    make_task(session, "TASK-MEM-EP", owner="backend-agent")
    _make_memory(
        session,
        agent_id="backend-agent",
        memory_type="episode",
        key="episode/TASK-001",
        content="Built /stats endpoint.",
    )
    _make_memory(
        session,
        agent_id="backend-agent",
        memory_type="skill",
        key="skill/db-pattern/TASK-001",
        content="Always use db_session dep.",
    )
    pkg = build_context_package(session, "TASK-MEM-EP", tmp_path)
    am = pkg["agent_memory"]
    assert "Built /stats endpoint." in am["episodes"]
    assert "Always use db_session dep." in am["skills"]


def test_context_packager_does_not_bleed_across_agents(session, tmp_path):
    """Memories for agent A are not injected into agent B's context."""
    make_task(session, "TASK-MEM-BLD", owner="backend-agent")
    _make_memory(
        session,
        agent_id="frontend-agent",
        memory_type="identity",
        key="identity",
        content="Frontend identity.",
    )
    pkg = build_context_package(session, "TASK-MEM-BLD", tmp_path)
    assert "agent_memory" not in pkg


def test_context_packager_size_warning(session, tmp_path):
    """A _warning key appears when combined memory exceeds 5000 chars."""
    make_task(session, "TASK-MEM-WARN", owner="claude-code-agent")
    big = "x" * 5001
    _make_memory(
        session,
        agent_id="claude-code-agent",
        memory_type="identity",
        key="identity",
        content=big[:2000],
    )
    _make_memory(
        session,
        agent_id="claude-code-agent",
        memory_type="episode",
        key="episode/TASK-999",
        content=big[:2000],
    )
    _make_memory(
        session,
        agent_id="claude-code-agent",
        memory_type="episode",
        key="episode/TASK-998",
        content=big[:2000],
    )
    pkg = build_context_package(session, "TASK-MEM-WARN", tmp_path)
    assert "_warning" in pkg["agent_memory"]
    assert "5k" in pkg["agent_memory"]["_warning"] or "chars" in pkg["agent_memory"]["_warning"]


def test_context_packager_no_warning_below_threshold(session, tmp_path):
    """No _warning when combined memory is under 5000 chars."""
    make_task(session, "TASK-MEM-OK", owner="claude-code-agent")
    _make_memory(
        session,
        agent_id="claude-code-agent",
        memory_type="identity",
        key="identity",
        content="Short identity.",
    )
    pkg = build_context_package(session, "TASK-MEM-OK", tmp_path)
    assert "_warning" not in pkg.get("agent_memory", {})


# ---------------------------------------------------------------------------
# Orchestrator API tests
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(engine):
    from orchestrator.orchestrator.db import get_session_factory

    app.dependency_overrides = {}

    from orchestrator.orchestrator import api as api_module

    api_module._SessionFactory = get_session_factory(engine)

    with TestClient(app) as c:
        yield c

    api_module._SessionFactory = None


def test_list_agent_memories_empty(api_client):
    resp = api_client.get("/agent-memories", params={"agent_id": "no-such-agent"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_agent_memories_filter_by_type(session, api_client):
    _make_memory(session, agent_id="test-list-agent", memory_type="identity", key="identity")
    _make_memory(
        session, agent_id="test-list-agent", memory_type="episode", key="episode/T1", content="ep1"
    )
    session.commit()

    resp = api_client.get(
        "/agent-memories", params={"agent_id": "test-list-agent", "memory_type": "episode"}
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r["memory_type"] == "episode" for r in rows)


def test_delete_agent_memory_writes_audit(engine, api_client):
    """DELETE /agent-memories/{id} removes the row and writes Event + AuditRow."""
    from sqlalchemy.orm import Session

    # Insert memory in a committed transaction so the API can see it.
    mem_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with Session(engine) as setup:
        setup.begin()
        setup.add(
            AgentMemory(
                id=uuid.UUID(mem_id),
                agent_id="del-test-agent",
                project_id="default",
                memory_type="skill",
                key="skill/foo/T1-del",
                content="A skill.",
                created_at=now,
                updated_at=now,
            )
        )
        setup.commit()

    resp = api_client.request(
        "DELETE", f"/agent-memories/{mem_id}", json={"reason": "test cleanup"}
    )
    assert resp.status_code == 204

    # Verify in a fresh session: memory gone, audit present.
    with Session(engine) as verify:
        gone = verify.get(AgentMemory, uuid.UUID(mem_id))
        assert gone is None
        audit = verify.query(AuditRow).filter(AuditRow.action == "memory_delete").first()
        assert audit is not None
        assert audit.details["reason"] == "test cleanup"

    # Cleanup audit row so it doesn't affect other tests.
    with Session(engine) as cleanup:
        cleanup.begin()
        a = cleanup.query(AuditRow).filter(AuditRow.action == "memory_delete").first()
        if a:
            ev = cleanup.get(Event, a.event_id)
            cleanup.delete(a)
            if ev:
                cleanup.delete(ev)
        cleanup.commit()


def test_delete_agent_memory_404(api_client):
    fake_id = str(uuid.uuid4())
    resp = api_client.request("DELETE", f"/agent-memories/{fake_id}", json={"reason": "gone"})
    assert resp.status_code == 404
