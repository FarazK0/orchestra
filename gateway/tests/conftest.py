"""Test fixtures for the gateway package.

Re-exports the shared DB fixtures from orchestrator/tests/conftest.py and
adds a make_run() helper that inserts a Run row. Gateway tests need an active
run in 'running' task state to satisfy the permission check.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

# Re-export session-scoped engine and per-test session from the orchestrator fixtures.
from orchestrator.tests.conftest import engine, make_task, session  # noqa: F401


@pytest.fixture(autouse=True)
def _clear_capability_secret(monkeypatch):
    """Remove CAPABILITY_SECRET so gateway tests don't require JWT tokens.

    Scope tests that need the secret add it back themselves via patch.dict.
    """
    monkeypatch.delenv("CAPABILITY_SECRET", raising=False)


if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def make_run(
    sess: "Session",
    task_id: str,
    agent_id: str = "backend-agent",
    branch: str | None = None,
    context_package_ref: str = "/tmp/ctx.json",
) -> "Run":  # noqa: F821
    """Insert a Run row and return the ORM object.

    Callers must also have a Task row in 'running' status for the same
    task_id (use make_task + a status override or transition).
    """
    from orchestrator.orchestrator.db import Run

    run = Run(
        run_id=uuid.uuid4(),
        schema_version=1,
        task_id=task_id,
        agent_id=agent_id,
        branch=branch or f"agent/backend/{task_id}",
        context_package_ref=context_package_ref,
        started_at=datetime.now(timezone.utc),
    )
    sess.add(run)
    sess.flush()
    return run
