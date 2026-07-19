"""Task validator.

Runs ruff + pytest against an agent's branch and writes the result back to
the control plane. The validator is orchestrator infrastructure (not an agent),
so it calls subprocess directly rather than routing through the gateway.

Usage:
    from orchestrator.orchestrator.validator import validate_task
    result = validate_task(session, task_id="TASK-001", repo_path="/path/to/repo")
    session.commit()
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Run, Task
from .state_machine import TaskNotFoundError, transition


class ValidationError(Exception):
    """Raised when validate_task cannot proceed (wrong status, git failure, etc.)."""


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _run_check(repo: Path, cmd: list[str]) -> dict:
    """Run a command in *repo* and return a result dict."""
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=120)
        return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "timed out after 120s"}


def validate_task(
    session: Session,
    task_id: str,
    repo_path: str,
    actor: str = "validator",
) -> dict:
    """Validate the agent's branch for a completed task.

    Steps:
      1. Checkout the agent branch for task_id (derived from the most recent run record).
      2. Run ``ruff check .``
      3. Run ``pytest``
      4. Transition task: completed → validated (all pass) or completed → failed.
      5. Update the most recent Run row's result field.

    Returns a results dict with keys: passed, branch, ruff, pytest, checkout_error.
    The caller owns the DB transaction and must call session.commit().

    Raises:
        TaskNotFoundError: task_id does not exist.
        ValidationError:   task is not in 'completed' status.
    """
    task = session.get(Task, task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id!r} not found")
    if task.status != "completed":
        raise ValidationError(
            f"Task {task_id!r} must be 'completed' to validate; current: {task.status!r}"
        )

    run_row = (
        session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.started_at.desc()))
        .scalars()
        .first()
    )
    branch = run_row.branch if run_row is not None else f"agent/backend/{task_id}"
    repo = Path(repo_path).resolve()

    results: dict = {
        "branch": branch,
        "ruff": None,
        "pytest": None,
        "checkout_error": None,
    }

    # 1. Checkout the agent branch.
    rc, _out, err = _git(repo, "checkout", branch)
    if rc != 0:
        results["checkout_error"] = err or f"git checkout {branch} failed (rc={rc})"
        _finalize(session, task_id, passed=False, actor=actor, results=results)
        return {**results, "passed": False}

    # 2. Ruff check.
    results["ruff"] = _run_check(repo, [sys.executable, "-m", "ruff", "check", "."])

    # 3. Pytest.
    results["pytest"] = _run_check(repo, [sys.executable, "-m", "pytest", "--tb=short", "-q"])

    pytest_rc = results["pytest"]["returncode"]
    passed = results["ruff"]["returncode"] == 0 and pytest_rc in (0, 5)

    try:
        from .metrics import validator_results_total

        validator_results_total.labels(
            result="passed" if passed else "failed", owner=task.owner
        ).inc()
    except Exception:
        pass

    _finalize(session, task_id, passed=passed, actor=actor, results=results)
    return {**results, "passed": passed}


def _finalize(
    session: Session,
    task_id: str,
    passed: bool,
    actor: str,
    results: dict,
) -> None:
    """Transition the task and update the run record."""
    new_status = "validated" if passed else "failed"
    transition(
        session,
        task_id,
        new_status,
        actor=actor,
        payload={"validation_passed": passed},
        details={k: v for k, v in results.items() if k != "ruff" or v is not None},
    )

    # Update the most recent Run (best-effort; no run is fine).
    run = (
        session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.started_at.desc()))
        .scalars()
        .first()
    )
    if run is not None:
        run.result = "validated" if passed else "validation_failed"
        if run.finished_at is None:
            run.finished_at = datetime.now(timezone.utc)
        session.flush()
