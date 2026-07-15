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
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Generator

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from schemas.models import Event as EventSchema
from schemas.models import RunRecord as RunRecordSchema
from schemas.models import Task as TaskSchema
from schemas.models import TaskBudget

from .db import Event as EventORM
from .db import Task as TaskORM
from .db import get_engine, get_session_factory
from .state_machine import InvalidTransitionError, TaskNotFoundError, transition

app = FastAPI(title="Orchestra Orchestrator", version="0.1.0")

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
    risk_tier: Annotated[int, Field(ge=0, le=2)] = 1
    budget: TaskBudget = Field(
        default_factory=lambda: TaskBudget(tokens=100_000, wall_clock_min=30, retries=2)
    )


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
        risk_tier=body.risk_tier,
        budget=body.budget.model_dump(),
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
) -> TaskSchema:
    try:
        transition(
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
    return TaskSchema.model_validate(session.get(TaskORM, task_id))


@app.get("/tasks/{task_id}/events", response_model=list[EventSchema])
def list_task_events(task_id: str, session: SessionDep) -> list[EventSchema]:
    _task_or_404(session, task_id)
    rows = (
        session.execute(
            select(EventORM).where(EventORM.task_id == task_id).order_by(EventORM.emitted_at)
        )
        .scalars()
        .all()
    )
    return [EventSchema.model_validate(e) for e in rows]


@app.post("/tasks/{task_id}/run", response_model=RunRecordSchema, status_code=201)
def start_run(task_id: str, body: RunRequest, session: SessionDep) -> RunRecordSchema:
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

    transition(
        session,
        task_id,
        "running",
        actor=body.agent_id,
        payload={"run_id": str(run.run_id)},
        details={"context_package_ref": run.context_package_ref},
    )

    return RunRecordSchema.model_validate(run)


@app.post("/tasks/{task_id}/validate", response_model=TaskSchema)
def validate_task_endpoint(
    task_id: str,
    body: ValidateRequest,
    session: SessionDep,
) -> TaskSchema:
    """Run ruff + pytest on the agent branch and transition to validated/failed."""
    from .validator import ValidationError, validate_task

    _task_or_404(session, task_id)
    try:
        validate_task(session, task_id, body.repo_path, actor=body.actor)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return TaskSchema.model_validate(session.get(TaskORM, task_id))
