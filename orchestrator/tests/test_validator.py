"""Tests for the validator (ruff + pytest against the agent branch).

Tests create a real temporary git repo rather than mocking subprocess,
so ruff and pytest must be available (they are: orchestra dev deps).
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.orchestrator.db import Run, Task
from orchestrator.orchestrator.validator import ValidationError, validate_task

from .conftest import make_task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo sitting on 'main'."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    # Add a simple passing file on main
    (repo / "health.py").write_text('def health() -> dict:\n    return {"status": "ok"}\n')
    (repo / "test_health.py").write_text(
        'from health import health\n\n\ndef test_health():\n    assert health() == {"status": "ok"}\n'
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _make_agent_branch(repo: Path, task_id: str, extra_files: dict[str, str] | None = None) -> None:
    """Create agent/backend/{task_id} with optional extra files and commit."""
    _git(repo, "checkout", "-b", f"agent/backend/{task_id}")
    if extra_files:
        for name, content in extra_files.items():
            (repo / name).write_text(content)
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"[{task_id}] agent changes")
    _git(repo, "checkout", "main")


def _make_run(session, task_id: str) -> Run:
    run = Run(
        run_id=uuid.uuid4(),
        schema_version=1,
        task_id=task_id,
        agent_id="backend-agent",
        branch=f"agent/backend/{task_id}",
        context_package_ref="/tmp/ctx.json",
        started_at=datetime.now(timezone.utc),
        result=None,
        tokens_used=0,
        cost_usd=0,
    )
    session.add(run)
    session.flush()
    return run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidateTask:
    def test_passes_clean_branch(self, session, git_repo):
        """Agent branch with valid code: ruff passes, pytest passes → validated."""
        task_id = "TASK-V01"
        make_task(session, task_id, status="completed")
        _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is True
        assert result["ruff"]["returncode"] == 0
        assert result["pytest"]["returncode"] == 0

        task = session.get(Task, task_id)
        assert task.status == "validated"

    def test_transitions_run_result_on_pass(self, session, git_repo):
        """Run.result is set to 'validated' on success."""
        task_id = "TASK-V02"
        make_task(session, task_id, status="completed")
        run = _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        validate_task(session, task_id, str(git_repo))

        session.refresh(run)
        assert run.result == "validated"

    def test_fails_ruff(self, session, git_repo):
        """Branch with an unused import: ruff fails → task transitions to failed."""
        task_id = "TASK-V03"
        make_task(session, task_id, status="completed")
        _make_run(session, task_id)
        # Write a file that imports something unused (F401)
        _make_agent_branch(
            git_repo,
            task_id,
            extra_files={"bad.py": "import os\n\nx = 1\n"},
        )

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is False
        assert result["ruff"]["returncode"] != 0

        task = session.get(Task, task_id)
        assert task.status == "failed"

    def test_fails_pytest(self, session, git_repo):
        """Branch with a failing test: pytest fails → task transitions to failed."""
        task_id = "TASK-V04"
        make_task(session, task_id, status="completed")
        _make_run(session, task_id)
        _make_agent_branch(
            git_repo,
            task_id,
            extra_files={"test_fail.py": "def test_broken():\n    assert False\n"},
        )

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is False
        assert result["pytest"]["returncode"] != 0

        task = session.get(Task, task_id)
        assert task.status == "failed"

    def test_transitions_run_result_on_fail(self, session, git_repo):
        """Run.result is set to 'validation_failed' on failure."""
        task_id = "TASK-V05"
        make_task(session, task_id, status="completed")
        run = _make_run(session, task_id)
        _make_agent_branch(
            git_repo,
            task_id,
            extra_files={"test_fail.py": "def test_broken():\n    assert False\n"},
        )

        validate_task(session, task_id, str(git_repo))

        session.refresh(run)
        assert run.result == "validation_failed"

    def test_raises_when_task_not_completed(self, session, git_repo):
        """validate_task raises ValidationError if status != completed."""
        task_id = "TASK-V06"
        make_task(session, task_id, status="running")

        with pytest.raises(ValidationError, match="must be 'completed'"):
            validate_task(session, task_id, str(git_repo))

        # Task status unchanged
        task = session.get(Task, task_id)
        assert task.status == "running"

    def test_raises_when_task_not_found(self, session, git_repo):
        """validate_task raises TaskNotFoundError for a missing task."""
        from orchestrator.orchestrator.state_machine import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            validate_task(session, "TASK-MISSING", str(git_repo))

    def test_fails_when_branch_missing(self, session, git_repo):
        """If the agent branch doesn't exist, checkout fails → task goes to failed."""
        task_id = "TASK-V07"
        make_task(session, task_id, status="completed")
        # No agent branch created

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is False
        assert result["checkout_error"] is not None

        task = session.get(Task, task_id)
        assert task.status == "failed"

    def test_writes_event_on_pass(self, session, git_repo):
        """A TASK_VALIDATED event is written to the events table."""
        from sqlalchemy import select
        from orchestrator.orchestrator.db import Event

        task_id = "TASK-V08"
        make_task(session, task_id, status="completed")
        _make_agent_branch(git_repo, task_id)

        validate_task(session, task_id, str(git_repo))

        events = (
            session.execute(
                select(Event)
                .where(Event.task_id == task_id)
                .where(Event.event_type == "TASK_VALIDATED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["validation_passed"] is True

    def test_writes_event_on_fail(self, session, git_repo):
        """A TASK_FAILED event is written when validation fails."""
        from sqlalchemy import select
        from orchestrator.orchestrator.db import Event

        task_id = "TASK-V09"
        make_task(session, task_id, status="completed")
        _make_agent_branch(
            git_repo,
            task_id,
            extra_files={"bad.py": "import os\n\nx = 1\n"},
        )

        validate_task(session, task_id, str(git_repo))

        events = (
            session.execute(
                select(Event)
                .where(Event.task_id == task_id)
                .where(Event.event_type == "TASK_FAILED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["validation_passed"] is False

    # ---------------------------------------------------------------------------
    # Provenance gate tests
    # ---------------------------------------------------------------------------

    def test_rejects_external_provenance_output(self, session, git_repo):
        """validate_task raises ValidationError when an output has provenance=external."""
        from orchestrator.orchestrator.db import ArtifactProvenance

        task_id = "TASK-V10"
        task = make_task(session, task_id, status="completed")
        task.outputs = ["report.md"]
        session.flush()

        _make_agent_branch(git_repo, task_id, extra_files={"report.md": "# scraped\n"})

        prov = ArtifactProvenance(
            repo_path=str(git_repo),
            file_path="report.md",
            provenance="external",
            set_by_task=task_id,
            set_at=datetime.now(timezone.utc),
        )
        session.add(prov)
        session.flush()

        with pytest.raises(ValidationError, match="external-provenance"):
            validate_task(session, task_id, str(git_repo))

        session.refresh(task)
        assert task.status == "completed"

    def test_agent_provenance_does_not_block_validation(self, session, git_repo):
        """validate_task succeeds when output has provenance=agent."""
        from orchestrator.orchestrator.db import ArtifactProvenance

        task_id = "TASK-V11"
        task = make_task(session, task_id, status="completed")
        task.outputs = ["output.py"]
        session.flush()

        _make_agent_branch(git_repo, task_id, extra_files={"output.py": "x = 1\n"})
        _make_run(session, task_id)

        prov = ArtifactProvenance(
            repo_path=str(git_repo),
            file_path="output.py",
            provenance="agent",
            set_by_task=task_id,
            set_at=datetime.now(timezone.utc),
        )
        session.add(prov)
        session.flush()

        results = validate_task(session, task_id, str(git_repo))
        assert results["passed"] is True
        session.refresh(task)
        assert task.status == "validated"

    def test_no_provenance_row_does_not_block_validation(self, session, git_repo):
        """validate_task succeeds when no provenance row exists for the output."""
        task_id = "TASK-V12"
        task = make_task(session, task_id, status="completed")
        task.outputs = ["util.py"]
        session.flush()

        _make_agent_branch(git_repo, task_id, extra_files={"util.py": "x = 1\n"})
        _make_run(session, task_id)

        # No ArtifactProvenance row — absent row means "agent", OK to validate.
        results = validate_task(session, task_id, str(git_repo))
        assert results["passed"] is True
        session.refresh(task)
        assert task.status == "validated"
