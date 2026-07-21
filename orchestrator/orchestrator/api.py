"""FastAPI application for the Orchestra orchestrator (control plane).

Endpoints
---------
GET  /healthz
POST /tasks                        create a task
GET  /tasks                        list tasks (optional ?status= filter, repeatable)
GET  /tasks/{task_id}              get one task
POST /tasks/{task_id}/transition   advance the state machine
GET  /tasks/{task_id}/events       audit-trail events for a task
POST /tasks/{task_id}/run          assemble context package and start a run
POST /tasks/{task_id}/validate     run ruff + pytest and transition completed -> validated/failed
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import dotenv
from typing import Annotated, Any, Generator

import redis.exceptions
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from schemas.models import Event as EventSchema
from schemas.models import RunRecord as RunRecordSchema
from schemas.models import Task as TaskSchema
from schemas.models import TaskBudget

from .db import AgentMemory as AgentMemoryORM
from .db import AuditRow as AuditRowORM
from .db import Event as EventORM
from .db import Run as RunORM
from .db import Task as TaskORM
from .db import get_engine, get_session_factory
from .metrics import human_queue_latency_seconds, tasks_total
from .policy import get_policy
from .state_machine import InvalidTransitionError, TaskNotFoundError, transition
from .streams import ROOT_STREAM_KEY, StreamPublisher
from .telemetry import setup_tracing

log = logging.getLogger(__name__)

_metrics_exposed = False


@asynccontextmanager
async def _lifespan(app: FastAPI):
    dotenv.load_dotenv()
    # expose() adds the /metrics route — guarded so it only runs once across
    # multiple TestClient contexts in the same test process.
    global _metrics_exposed
    if not _metrics_exposed:
        setup_tracing(app, "orchestrator")
        _instrumentator.expose(app)
        _metrics_exposed = True
    get_policy()  # warm the cache; logs a warning if file absent
    yield


app = FastAPI(title="Orchestra Orchestrator", version="0.1.0", lifespan=_lifespan)

# instrument() adds Prometheus middleware — must run before the app's middleware
# stack is built (i.e., before the first request / TestClient.__enter__).
_instrumentator = Instrumentator().instrument(app)

# ---------------------------------------------------------------------------
# Best-effort Redis event publish
# ---------------------------------------------------------------------------

_publisher: StreamPublisher | None = None
_root_publisher: StreamPublisher | None = None


def _try_publish(event_id: str, event_type: str, task_id: str, payload: dict) -> None:
    global _publisher
    if _publisher is None:
        _publisher = StreamPublisher()
    try:
        _publisher.publish(event_id, event_type, task_id, payload)
    except redis.exceptions.RedisError as exc:
        log.warning("Redis publish failed for event %s: %s", event_id, exc)


def _try_publish_replan(event_id: str, payload: dict) -> None:
    global _root_publisher
    if _root_publisher is None:
        _root_publisher = StreamPublisher()
    try:
        _root_publisher.publish(
            event_id,
            "PLAN_REPLAN_REQUESTED",
            payload.get("trigger_task_id", ""),
            payload,
            stream_key=ROOT_STREAM_KEY,
        )
    except redis.exceptions.RedisError as exc:
        log.warning("Root stream publish failed for replan %s: %s", event_id, exc)


# ---------------------------------------------------------------------------
# DB session dependency
# ---------------------------------------------------------------------------

_SessionFactory = None


def _factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = get_session_factory(get_engine())
    return _SessionFactory


def get_session() -> Generator[Session, None, None]:
    sess = _factory()()
    sess.begin()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


SessionDep = Annotated[Session, Depends(get_session)]

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    title: str
    owner: str = "human"
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    risk_tier: Annotated[int | None, Field(ge=0, le=2)] = None
    budget: TaskBudget = Field(
        default_factory=lambda: TaskBudget(tokens=100_000, wall_clock_min=30, retries=2)
    )
    # v0.3 adaptive lifecycle
    parent_task_id: str | None = None
    spawn_depth: int = 0


class TransitionRequest(BaseModel):
    new_status: str
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    agent_id: str = "backend-agent"
    repo_path: str
    store_dir: str | None = None


class ValidateRequest(BaseModel):
    repo_path: str
    actor: str = "validator"


class ValidateResponse(BaseModel):
    task: TaskSchema
    validation: dict[str, Any]


class AgentMemorySchema(BaseModel):
    id: str
    agent_id: str
    project_id: str
    memory_type: str
    key: str
    content: str
    source_task_id: str | None
    created_at: str
    updated_at: str
    last_used_at: str | None

    @classmethod
    def from_orm(cls, m: Any) -> "AgentMemorySchema":
        return cls(
            id=str(m.id),
            agent_id=m.agent_id,
            project_id=m.project_id,
            memory_type=m.memory_type,
            key=m.key,
            content=m.content,
            source_task_id=m.source_task_id,
            created_at=m.created_at.isoformat(),
            updated_at=m.updated_at.isoformat(),
            last_used_at=m.last_used_at.isoformat() if m.last_used_at else None,
        )


class MemoryDeleteRequest(BaseModel):
    reason: str = "human deleted"


class ReplanRequest(BaseModel):
    trigger_task_id: str
    child_task_id: str
    reason: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _next_task_id(session: Session) -> str:
    # Phase 1: safe for single-human use; replace with a DB sequence in Phase 2.
    n = session.execute(
        text("SELECT COALESCE(MAX(CAST(SUBSTR(id, 6) AS INTEGER)), 0) FROM tasks")
    ).scalar()
    return f"TASK-{(n or 0) + 1:03d}"


def _task_or_404(session: Session, task_id: str) -> TaskORM:
    task = session.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    return task


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", response_model=TaskSchema, status_code=201)
def create_task(body: TaskCreate, session: SessionDep) -> TaskSchema:
    now = datetime.now(timezone.utc)
    # Policy determines the tier from output paths (default_tier=1 when no rule matches).
    # An explicit risk_tier in the request acts as a floor — it can raise above policy but
    # cannot lower below it (prevents accidentally downgrading a migration to tier 0).
    policy_tier = get_policy().tier_for_outputs(body.outputs)
    effective_tier = max(body.risk_tier, policy_tier) if body.risk_tier is not None else policy_tier
    task = TaskORM(
        id=_next_task_id(session),
        schema_version=1,
        title=body.title,
        owner=body.owner,
        status="created",
        depends_on=body.depends_on,
        inputs=body.inputs,
        outputs=body.outputs,
        acceptance=body.acceptance,
        risk_tier=effective_tier,
        budget=body.budget.model_dump(),
        parent_task_id=body.parent_task_id,
        spawn_depth=body.spawn_depth,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    return TaskSchema.model_validate(task)


@app.get("/tasks", response_model=list[TaskSchema])
def list_tasks(
    session: SessionDep,
    status: list[str] = Query(default=[]),
) -> list[TaskSchema]:
    q = select(TaskORM).order_by(TaskORM.created_at)
    if status:
        q = q.where(TaskORM.status.in_(status))
    rows = session.execute(q).scalars().all()
    return [TaskSchema.model_validate(t) for t in rows]


@app.get("/tasks/{task_id}", response_model=TaskSchema)
def get_task(task_id: str, session: SessionDep) -> TaskSchema:
    return TaskSchema.model_validate(_task_or_404(session, task_id))


@app.post("/tasks/{task_id}/transition", response_model=TaskSchema)
def transition_task(
    task_id: str,
    body: TransitionRequest,
    session: SessionDep,
    bg: BackgroundTasks,
) -> TaskSchema:
    try:
        event = transition(
            session,
            task_id,
            body.new_status,
            actor=body.actor,
            payload=body.payload,
            details=body.details,
        )
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # If the caller supplied a checkpoint (e.g. running → suspended), persist it.
    task = session.get(TaskORM, task_id)
    if body.details.get("checkpoint") is not None:
        task.checkpoint = body.details["checkpoint"]
    bg.add_task(_try_publish, str(event.event_id), event.event_type, task_id, event.payload)
    tasks_total.labels(new_status=body.new_status, owner=task.owner).inc()
    if body.new_status == "merged":
        validated_event = session.execute(
            select(EventORM)
            .where(EventORM.task_id == task_id, EventORM.event_type == "TASK_VALIDATED")
            .order_by(EventORM.emitted_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if validated_event is not None:
            latency = (datetime.now(timezone.utc) - validated_event.emitted_at).total_seconds()
            human_queue_latency_seconds.labels(owner=task.owner).observe(latency)
    return TaskSchema.model_validate(task)


@app.get("/tasks/{task_id}/events", response_model=list[EventSchema])
def list_task_events(
    task_id: str,
    session: SessionDep,
    event_type: str | None = Query(default=None),
) -> list[EventSchema]:
    _task_or_404(session, task_id)
    q = select(EventORM).where(EventORM.task_id == task_id).order_by(EventORM.emitted_at)
    if event_type:
        q = q.where(EventORM.event_type == event_type)
    rows = session.execute(q).scalars().all()
    return [EventSchema.model_validate(e) for e in rows]


@app.post("/tasks/{task_id}/run", response_model=RunRecordSchema, status_code=201)
def start_run(
    task_id: str, body: RunRequest, session: SessionDep, bg: BackgroundTasks
) -> RunRecordSchema:
    """Assemble the context package, create a Run row, transition task to running."""
    from .context_packager import (
        TaskNotFoundError as PackagerNotFound,
        create_run,
    )

    task = _task_or_404(session, task_id)
    if task.status != "assigned":
        raise HTTPException(
            status_code=409,
            detail=(f"Task must be in 'assigned' status to start a run; current: {task.status!r}"),
        )

    repo_path = Path(body.repo_path)
    store_dir = Path(body.store_dir) if body.store_dir else repo_path / ".orchestra" / "context"

    try:
        run = create_run(session, task_id, body.agent_id, repo_path, store_dir)
    except PackagerNotFound:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    event = transition(
        session,
        task_id,
        "running",
        actor=body.agent_id,
        payload={"run_id": str(run.run_id)},
        details={"context_package_ref": run.context_package_ref},
    )
    bg.add_task(_try_publish, str(event.event_id), event.event_type, task_id, event.payload)

    return RunRecordSchema.model_validate(run)


@app.post("/tasks/{task_id}/validate", response_model=ValidateResponse)
def validate_task_endpoint(
    task_id: str,
    body: ValidateRequest,
    session: SessionDep,
    bg: BackgroundTasks,
) -> ValidateResponse:
    """Run ruff + pytest on the agent branch and transition to validated/failed."""
    from .validator import ValidationError, validate_task

    _task_or_404(session, task_id)
    try:
        results = validate_task(session, task_id, body.repo_path, actor=body.actor)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # validate_task() calls transition() internally; fetch the event it emitted.
    latest_event = (
        session.execute(
            select(EventORM).where(EventORM.task_id == task_id).order_by(EventORM.emitted_at.desc())
        )
        .scalars()
        .first()
    )
    if latest_event is not None:
        bg.add_task(
            _try_publish,
            str(latest_event.event_id),
            latest_event.event_type,
            task_id,
            latest_event.payload,
        )
    return ValidateResponse(
        task=TaskSchema.model_validate(session.get(TaskORM, task_id)),
        validation=results,
    )


@app.get("/tasks/{task_id}/runs")
def list_task_runs(task_id: str, session: SessionDep) -> list[dict]:
    """Return run records for a task, newest first."""
    _task_or_404(session, task_id)
    rows = (
        session.execute(
            select(RunORM).where(RunORM.task_id == task_id).order_by(RunORM.started_at.desc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "run_id": str(r.run_id),
            "agent_id": r.agent_id,
            "branch": r.branch,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "result": r.result,
            "log_path": r.log_path,
            "tokens_used": r.tokens_used,
            "cost_usd": float(r.cost_usd),
        }
        for r in rows
    ]


@app.get("/tasks/{task_id}/audit")
def list_task_audit(task_id: str, session: SessionDep) -> list[dict]:
    """Return the 50 most recent audit rows for a task, newest first."""
    _task_or_404(session, task_id)
    rows = (
        session.execute(
            select(AuditRowORM)
            .where(AuditRowORM.task_id == task_id)
            .order_by(AuditRowORM.timestamp.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "timestamp": r.timestamp.isoformat(),
            "actor": r.actor,
            "action": r.action,
            "details": r.details,
        }
        for r in rows
    ]


@app.post("/scheduler/replan", status_code=202)
def trigger_replan(body: ReplanRequest, bg: BackgroundTasks) -> dict:
    """Request the root agent to re-evaluate the task plan after a task discovery.

    Publishes PLAN_REPLAN_REQUESTED to the root:requests stream.  The root agent
    consumes the event and decides whether to add or re-order pending tasks.
    Returns immediately; publishing happens in a background task.
    """
    import uuid as _uuid

    event_id = str(_uuid.uuid4())
    payload = {
        "trigger_task_id": body.trigger_task_id,
        "child_task_id": body.child_task_id,
        "reason": body.reason,
    }
    bg.add_task(_try_publish_replan, event_id, payload)
    log.info("Replan queued: trigger=%s child=%s", body.trigger_task_id, body.child_task_id)
    return {"status": "queued", "event_id": event_id}


@app.get("/agent-memories", response_model=list[AgentMemorySchema])
def list_agent_memories(
    session: SessionDep,
    agent_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    project_id: str = Query(default="default"),
) -> list[AgentMemorySchema]:
    """List agent memory entries, optionally filtered by agent_id and memory_type."""
    q = select(AgentMemoryORM).where(AgentMemoryORM.project_id == project_id)
    if agent_id:
        q = q.where(AgentMemoryORM.agent_id == agent_id)
    if memory_type:
        q = q.where(AgentMemoryORM.memory_type == memory_type)
    q = q.order_by(AgentMemoryORM.updated_at)
    rows = session.execute(q).scalars().all()
    return [AgentMemorySchema.from_orm(m) for m in rows]


@app.delete("/agent-memories/{memory_id}", status_code=204)
def delete_agent_memory(
    memory_id: str,
    body: MemoryDeleteRequest,
    session: SessionDep,
    bg: BackgroundTasks,
) -> None:
    """Delete a memory entry. Writes an audit event before deletion."""
    import uuid as _uuid
    from datetime import datetime, timezone

    mem = session.get(AgentMemoryORM, _uuid.UUID(memory_id))
    if mem is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id!r} not found")

    now = datetime.now(timezone.utc)
    event = EventORM(
        event_id=_uuid.uuid4(),
        schema_version=1,
        event_type="AGENT_MEMORY_DELETED",
        task_id=mem.source_task_id,
        emitted_by="human",
        emitted_at=now,
        payload={
            "memory_id": memory_id,
            "agent_id": mem.agent_id,
            "memory_type": mem.memory_type,
            "key": mem.key,
            "reason": body.reason,
        },
    )
    session.add(event)
    session.flush()

    from .db import AuditRow

    audit = AuditRow(
        id=_uuid.uuid4(),
        timestamp=now,
        actor="human",
        action="memory_delete",
        task_id=mem.source_task_id,
        event_id=event.event_id,
        details={"memory_id": memory_id, "agent_id": mem.agent_id, "reason": body.reason},
    )
    session.add(audit)
    session.delete(mem)

    bg.add_task(
        _try_publish,
        str(event.event_id),
        "AGENT_MEMORY_DELETED",
        mem.source_task_id or "",
        event.payload,
    )
