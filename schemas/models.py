"""Pydantic v2 models derived from the JSON schemas in this directory.

Each class maps 1-to-1 to a *.schema.json file and enforces the same
constraints (patterns, enum values, ranges, required fields).

All models carry `schema_version: Literal[1]` so that serialised payloads
can be version-checked at the consumer without importing this module.

`model_config = ConfigDict(from_attributes=True)` is set on models that
correspond to SQLAlchemy ORM rows, enabling `Model.model_validate(orm_obj)`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    created = "created"
    assigned = "assigned"
    running = "running"
    blocked = "blocked"
    suspended = "suspended"
    awaiting_human = "awaiting_human"
    completed = "completed"
    validated = "validated"
    merged = "merged"
    closed = "closed"
    failed = "failed"
    escalated = "escalated"
    cancelled = "cancelled"


class TaskBudget(BaseModel):
    tokens: int
    wall_clock_min: int
    retries: int


class Task(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(pattern=r"^TASK-[0-9]{3,}$")]
    title: str
    owner: str
    status: TaskStatus = TaskStatus.created
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    risk_tier: Annotated[int, Field(ge=0, le=2)]
    budget: TaskBudget
    # v0.3 adaptive lifecycle
    parent_task_id: str | None = None
    spawn_depth: int = 0
    blocked_by: list[str] = Field(default_factory=list)
    checkpoint: dict | None = None


# ---------------------------------------------------------------------------
# Event  (append-only; event_id is the idempotency key)
# ---------------------------------------------------------------------------


class Event(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schema_version: Literal[1] = 1
    event_id: UUID
    event_type: str
    task_id: str | None = None
    emitted_by: str
    emitted_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# AgentIdentity
# ---------------------------------------------------------------------------


class AgentIdentity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schema_version: Literal[1] = 1
    id: str
    role: str
    description: str
    skills: list[str]
    subscriptions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# RunRecord
# ---------------------------------------------------------------------------


class RunResult(str, Enum):
    success = "success"
    failed = "failed"
    timeout = "timeout"
    budget_exceeded = "budget_exceeded"


class RunRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    task_id: str
    agent_id: str
    branch: str
    context_package_ref: str
    started_at: datetime
    finished_at: datetime | None = None
    result: RunResult | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Capability  (Phase 1: orchestrator-side allowlist; Phase 3: signed token)
# ---------------------------------------------------------------------------


class CapabilityScopes(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    execute: list[str] = Field(default_factory=list)
    emit: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schema_version: Literal[1] = 1
    capability_id: UUID
    task_id: str
    agent_id: str
    expires_at: datetime
    revoked: bool = False
    scopes: CapabilityScopes = Field(default_factory=CapabilityScopes)
