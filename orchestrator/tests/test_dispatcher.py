"""Integration tests for the Dispatcher.

Requires Docker Compose running (make up):
  - Postgres on port 5433
  - Redis on port 6380

Each test truncates the relevant tables via the clean_db fixture.
subprocess.Popen is monkeypatched so no real agent processes are launched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import Run, Task
from orchestrator.orchestrator.dispatcher import Dispatcher
from orchestrator.orchestrator.streams import STREAM_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_db(engine):
    """Truncate control-plane tables before each dispatcher test."""
    with Session(engine) as s:
        s.execute(
            text("TRUNCATE stream_deliveries, audit, runs, events, tasks RESTART IDENTITY CASCADE")
        )
        s.commit()
    yield
    with Session(engine) as s:
        s.execute(
            text("TRUNCATE stream_deliveries, audit, runs, events, tasks RESTART IDENTITY CASCADE")
        )
        s.commit()


@pytest.fixture
def tmp_repo(tmp_path):
    """Minimal managed repo directory with a docs/adr folder (required by context packager)."""
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def tmp_store(tmp_path):
    store = tmp_path / "runs"
    store.mkdir()
    return store


@pytest.fixture
def dispatcher(redis_url, session_factory, tmp_repo, tmp_store):
    return Dispatcher(redis_url, session_factory, tmp_repo, tmp_store)


def _make_task(
    session_factory,
    task_id: str,
    status: str,
    outputs: list[str] | None = None,
    retry_count: int = 0,
    budget_retries: int = 1,
):
    now = datetime.now(timezone.utc)
    with session_factory() as s:
        task = Task(
            id=task_id,
            schema_version=1,
            title="Test task",
            owner="backend-agent",
            status=status,
            depends_on=[],
            inputs=[],
            outputs=outputs or [],
            acceptance=[],
            risk_tier=1,
            budget={"tokens": 10_000, "wall_clock_min": 5, "retries": budget_retries},
            retry_count=retry_count,
            created_at=now,
            updated_at=now,
        )
        s.add(task)
        s.commit()
        return task.id


# ---------------------------------------------------------------------------
# _on_task_assigned
# ---------------------------------------------------------------------------


def test_on_task_assigned_creates_run_and_launches_agent(
    clean_db, redis_client, session_factory, dispatcher
):
    task_id = _make_task(session_factory, "TASK-DIS01", "assigned")

    with patch("orchestrator.orchestrator.dispatcher.subprocess.Popen") as mock_popen:
        with session_factory() as session:
            dispatcher._on_task_assigned(task_id, session)

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert "-m" in cmd
    assert "agents.backend.main" in cmd

    with session_factory() as s:
        task = s.get(Task, task_id)
        assert task.status == "running"
        run = (
            s.execute(__import__("sqlalchemy").select(Run).where(Run.task_id == task_id))
            .scalars()
            .first()
        )
        assert run is not None


def test_on_task_assigned_skips_non_assigned_task(
    clean_db, redis_client, session_factory, dispatcher
):
    task_id = _make_task(session_factory, "TASK-DIS02", "created")

    with patch("orchestrator.orchestrator.dispatcher.subprocess.Popen") as mock_popen:
        with session_factory() as session:
            dispatcher._on_task_assigned(task_id, session)

    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------


def test_concurrency_guard_skips_conflicting_task(
    clean_db, redis_client, session_factory, dispatcher
):
    _make_task(session_factory, "TASK-DIS10", "running", outputs=["src/auth/"])
    blocked_id = _make_task(
        session_factory, "TASK-DIS11", "assigned", outputs=["src/auth/login.py"]
    )

    with patch("orchestrator.orchestrator.dispatcher.subprocess.Popen") as mock_popen:
        with session_factory() as session:
            dispatcher._on_task_assigned(blocked_id, session)

    mock_popen.assert_not_called()

    with session_factory() as s:
        task = s.get(Task, blocked_id)
        assert task.status == "assigned"  # unchanged


# ---------------------------------------------------------------------------
# _on_task_completed — DAG successor dispatch
# ---------------------------------------------------------------------------


def test_on_task_completed_auto_assigns_successor(
    clean_db, redis_client, session_factory, dispatcher
):
    """Completing task A should auto-transition task B (depends on A) to assigned."""
    a_id = _make_task(session_factory, "TASK-DIS20", "completed")
    now = datetime.now(timezone.utc)
    with session_factory() as s:
        b = Task(
            id="TASK-DIS21",
            schema_version=1,
            title="Successor",
            owner="backend-agent",
            status="created",
            depends_on=[a_id],
            inputs=[],
            outputs=[],
            acceptance=[],
            risk_tier=1,
            budget={"tokens": 10_000, "wall_clock_min": 5, "retries": 1},
            created_at=now,
            updated_at=now,
        )
        s.add(b)
        s.commit()

    with session_factory() as session:
        dispatcher._on_task_completed(a_id, session)

    with session_factory() as s:
        b = s.get(Task, "TASK-DIS21")
        assert b.status == "assigned"

    # TASK_ASSIGNED should have been published to Redis
    messages = dispatcher._publisher._r.xrange(STREAM_KEY)
    assigned_msgs = [m for _, m in messages if m.get("event_type") == "TASK_ASSIGNED"]
    assert len(assigned_msgs) >= 1


# ---------------------------------------------------------------------------
# _recover_stale
# ---------------------------------------------------------------------------


def test_recover_stale_republishes_assigned_task(
    clean_db, redis_client, session_factory, dispatcher
):
    """An assigned task with no active run should be re-published by _recover_stale."""
    task_id = _make_task(session_factory, "TASK-DIS30", "assigned")

    dispatcher._recover_stale()

    messages = dispatcher._publisher._r.xrange(STREAM_KEY)
    assigned_msgs = [m for _, m in messages if m.get("task_id") == task_id]
    assert len(assigned_msgs) >= 1


# ---------------------------------------------------------------------------
# _on_task_failed — retry policy
# ---------------------------------------------------------------------------


def test_on_task_failed_retries_within_budget(clean_db, redis_client, session_factory, dispatcher):
    """A failed task within its retry budget should be re-launched on a new run."""
    task_id = _make_task(session_factory, "TASK-FAIL01", "failed", budget_retries=2, retry_count=0)
    with patch("orchestrator.orchestrator.dispatcher.subprocess.Popen") as mock_popen:
        with session_factory() as session:
            dispatcher._on_task_failed(task_id, session)
    mock_popen.assert_called_once()
    with session_factory() as s:
        task = s.get(Task, task_id)
        assert task.status == "running"
        assert task.retry_count == 1


def test_on_task_failed_escalates_when_retries_exhausted(
    clean_db, redis_client, session_factory, dispatcher
):
    """A failed task that has used all retries should be escalated."""
    task_id = _make_task(session_factory, "TASK-FAIL02", "failed", budget_retries=2, retry_count=2)
    with patch("orchestrator.orchestrator.dispatcher.subprocess.Popen") as mock_popen:
        with session_factory() as session:
            dispatcher._on_task_failed(task_id, session)
    mock_popen.assert_not_called()
    with session_factory() as s:
        task = s.get(Task, task_id)
        assert task.status == "escalated"


def test_retry_branch_has_retry_suffix(clean_db, redis_client, session_factory, dispatcher):
    """The retry run's branch name should carry a -retry-N suffix."""
    import sqlalchemy

    task_id = _make_task(session_factory, "TASK-FAIL03", "failed", budget_retries=2, retry_count=0)
    with patch("orchestrator.orchestrator.dispatcher.subprocess.Popen"):
        with session_factory() as session:
            dispatcher._on_task_failed(task_id, session)
    with session_factory() as s:
        run = s.execute(sqlalchemy.select(Run).where(Run.task_id == task_id)).scalars().first()
        assert run is not None
        assert run.branch.endswith("-retry-1")


def test_on_task_failed_skips_non_failed_task(clean_db, redis_client, session_factory, dispatcher):
    """Handler is a no-op when the task is not in failed status (stale event guard)."""
    task_id = _make_task(session_factory, "TASK-FAIL04", "running")
    with patch("orchestrator.orchestrator.dispatcher.subprocess.Popen") as mock_popen:
        with session_factory() as session:
            dispatcher._on_task_failed(task_id, session)
    mock_popen.assert_not_called()
    with session_factory() as s:
        task = s.get(Task, task_id)
        assert task.status == "running"


def test_recover_stale_skips_task_with_active_run(
    clean_db, redis_client, session_factory, dispatcher
):
    """A task with an active (unfinished) run should not be re-published."""
    task_id = _make_task(session_factory, "TASK-DIS40", "assigned")
    now = datetime.now(timezone.utc)
    with session_factory() as s:
        s.add(
            Run(
                run_id=uuid.uuid4(),
                schema_version=1,
                task_id=task_id,
                agent_id="backend-agent",
                branch=f"agent/backend/{task_id}",
                context_package_ref="/tmp/fake.json",
                started_at=now,
                finished_at=None,
            )
        )
        s.commit()

    dispatcher._recover_stale()

    messages = dispatcher._publisher._r.xrange(STREAM_KEY)
    assigned_msgs = [m for _, m in messages if m.get("task_id") == task_id]
    assert len(assigned_msgs) == 0
