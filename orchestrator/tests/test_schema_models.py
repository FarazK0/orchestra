"""Tests for Pydantic models in schemas/models.py.

Verifies that each model enforces its JSON-schema constraints:
required fields, enum values, pattern validation, range checks,
and round-trip serialisation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from schemas.models import (
    AgentIdentity,
    Capability,
    Event,
    RunRecord,
    RunResult,
    Task,
    TaskStatus,
)

NOW = datetime.now(timezone.utc)
_BUDGET = {"tokens": 100_000, "wall_clock_min": 30, "retries": 2}


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


def _valid_task(**overrides) -> dict:
    base = {
        "id": "TASK-001",
        "title": "Implement login",
        "owner": "backend-agent",
        "risk_tier": 1,
        "budget": _BUDGET,
    }
    return {**base, **overrides}


def test_task_valid():
    t = Task(**_valid_task())
    assert t.schema_version == 1
    assert t.status == TaskStatus.created
    assert t.depends_on == []


def test_task_status_enum_roundtrip():
    for s in TaskStatus:
        t = Task(**_valid_task(status=s.value))
        assert t.status == s


def test_task_id_pattern_valid():
    Task(**_valid_task(id="TASK-100"))
    Task(**_valid_task(id="TASK-9999"))


def test_task_id_pattern_invalid():
    with pytest.raises(ValidationError, match="id"):
        Task(**_valid_task(id="TSK-001"))  # wrong prefix
    with pytest.raises(ValidationError, match="id"):
        Task(**_valid_task(id="TASK-01"))  # fewer than 3 digits
    with pytest.raises(ValidationError, match="id"):
        Task(**_valid_task(id="task-001"))  # lowercase


def test_task_risk_tier_bounds():
    Task(**_valid_task(risk_tier=0))
    Task(**_valid_task(risk_tier=2))
    with pytest.raises(ValidationError, match="risk_tier"):
        Task(**_valid_task(risk_tier=-1))
    with pytest.raises(ValidationError, match="risk_tier"):
        Task(**_valid_task(risk_tier=3))


def test_task_budget_required_fields():
    with pytest.raises(ValidationError):
        Task(**_valid_task(budget={"tokens": 1000}))  # missing wall_clock_min, retries


def test_task_list_fields_default_empty():
    t = Task(**_valid_task())
    assert t.inputs == []
    assert t.outputs == []
    assert t.acceptance == []
    assert t.depends_on == []


def test_task_serialise_deserialise():
    t = Task(**_valid_task(inputs=["artifacts/api.yaml"], acceptance=["lint passes"]))
    data = t.model_dump()
    t2 = Task.model_validate(data)
    assert t2 == t


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


def _valid_event(**overrides) -> dict:
    base = {
        "event_id": uuid.uuid4(),
        "event_type": "TASK_ASSIGNED",
        "task_id": "TASK-001",
        "emitted_by": "orchestrator",
        "emitted_at": NOW,
    }
    return {**base, **overrides}


def test_event_valid():
    e = Event(**_valid_event())
    assert e.schema_version == 1
    assert e.payload == {}


def test_event_task_id_nullable():
    e = Event(**_valid_event(task_id=None))
    assert e.task_id is None


def test_event_payload_stored():
    e = Event(**_valid_event(payload={"agent_id": "backend-agent"}))
    assert e.payload["agent_id"] == "backend-agent"


def test_event_event_id_is_uuid():
    raw_id = uuid.uuid4()
    e = Event(**_valid_event(event_id=raw_id))
    assert e.event_id == raw_id


def test_event_missing_required_raises():
    data = _valid_event()
    del data["event_type"]
    with pytest.raises(ValidationError, match="event_type"):
        Event(**data)


# ---------------------------------------------------------------------------
# AgentIdentity
# ---------------------------------------------------------------------------


def test_agent_identity_valid():
    a = AgentIdentity(
        id="backend-agent",
        role="backend",
        description="Implements API tasks",
        skills=["python", "fastapi"],
    )
    assert a.schema_version == 1
    assert a.subscriptions == []


def test_agent_identity_subscriptions():
    a = AgentIdentity(
        id="qa-agent",
        role="qa",
        description="QA",
        skills=["pytest"],
        subscriptions=["TASK_VALIDATED"],
    )
    assert "TASK_VALIDATED" in a.subscriptions


def test_agent_identity_missing_required():
    with pytest.raises(ValidationError, match="role"):
        AgentIdentity(id="x", description="d", skills=[])


# ---------------------------------------------------------------------------
# RunRecord
# ---------------------------------------------------------------------------


def _valid_run(**overrides) -> dict:
    base = {
        "run_id": uuid.uuid4(),
        "task_id": "TASK-001",
        "agent_id": "backend-agent",
        "branch": "agent/backend/TASK-001",
        "context_package_ref": "runs/abc123/context.json",
        "started_at": NOW,
    }
    return {**base, **overrides}


def test_run_record_valid():
    r = RunRecord(**_valid_run())
    assert r.schema_version == 1
    assert r.result is None
    assert r.tokens_used == 0
    assert r.cost_usd == 0.0


def test_run_record_result_enum():
    for result in RunResult:
        r = RunRecord(**_valid_run(result=result.value))
        assert r.result == result


def test_run_record_result_invalid():
    with pytest.raises(ValidationError, match="result"):
        RunRecord(**_valid_run(result="unknown"))


def test_run_record_finished_nullable():
    r = RunRecord(**_valid_run(finished_at=NOW, result="success"))
    assert r.finished_at == NOW


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def _valid_capability(**overrides) -> dict:
    base = {
        "capability_id": uuid.uuid4(),
        "task_id": "TASK-001",
        "agent_id": "backend-agent",
        "expires_at": NOW,
    }
    return {**base, **overrides}


def test_capability_valid():
    c = Capability(**_valid_capability())
    assert c.schema_version == 1
    assert c.revoked is False
    assert c.scopes.read == []


def test_capability_scopes():
    c = Capability(
        **_valid_capability(
            scopes={
                "read": ["artifacts/api.yaml"],
                "write": ["code/auth/"],
                "execute": [],
                "emit": [],
            }
        )
    )
    assert c.scopes.read == ["artifacts/api.yaml"]
    assert c.scopes.write == ["code/auth/"]


def test_capability_revoked_flag():
    c = Capability(**_valid_capability(revoked=True))
    assert c.revoked is True


def test_capability_missing_agent_id():
    data = _valid_capability()
    del data["agent_id"]
    with pytest.raises(ValidationError, match="agent_id"):
        Capability(**data)
