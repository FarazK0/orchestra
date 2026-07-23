"""Tests for the pluggable registry-driven validator.

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
from orchestrator.orchestrator.validator import (
    ValidationError,
    _check_file_exists,
    detect_validators,
    load_registry,
    validate_task,
)

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
# Registry + detect_validators unit tests (no DB needed)
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_load_registry_returns_dict(self):
        registry = load_registry()
        assert isinstance(registry, dict)
        assert "ruff" in registry
        assert "pytest" in registry
        assert "file-exists" in registry
        assert "llm-acceptance" in registry

    def test_detect_py_outputs(self):
        registry = load_registry()
        result = detect_validators(["app/main.py", "tests/test_app.py"], registry)
        assert "ruff" in result
        assert "pytest" in result
        assert "file-exists" not in result  # always_run, not included in detect list
        assert "llm-acceptance" not in result  # always_run

    def test_detect_js_outputs(self):
        registry = load_registry()
        result = detect_validators(["src/App.tsx", "src/App.test.tsx"], registry)
        assert "eslint" in result

    def test_detect_empty_outputs(self):
        registry = load_registry()
        result = detect_validators([], registry)
        assert result == []

    def test_detect_no_match(self):
        registry = load_registry()
        result = detect_validators(["README.md", "data.csv"], registry)
        assert result == []

    def test_mypy_excluded_from_autodetect(self):
        registry = load_registry()
        result = detect_validators(["app.py"], registry)
        assert "mypy" not in result  # auto_detect: false


# ---------------------------------------------------------------------------
# Built-in: file-exists unit tests
# ---------------------------------------------------------------------------


class TestFileExists:
    def test_passes_when_no_outputs(self, tmp_path):
        result = _check_file_exists(tmp_path, [])
        assert result.passed is True
        assert "No output files" in result.output

    def test_passes_when_all_present(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        result = _check_file_exists(tmp_path, ["a.py", "b.py"])
        assert result.passed is True
        assert "All 2" in result.output

    def test_fails_when_file_missing(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        result = _check_file_exists(tmp_path, ["a.py", "missing.py"])
        assert result.passed is False
        assert "missing.py" in result.output


# ---------------------------------------------------------------------------
# validate_task integration tests
# ---------------------------------------------------------------------------


class TestValidateTask:
    def test_passes_clean_branch(self, session, git_repo):
        """Agent branch with valid code: all checks pass → validated."""
        task_id = "TASK-V01"
        make_task(session, task_id, status="completed")
        _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is True
        assert "checks" in result
        assert result["summary"].endswith("checks passed")
        # All checks in the list must have passed=True
        assert all(c["passed"] for c in result["checks"])

        task = session.get(Task, task_id)
        assert task.status == "validated"

    def test_result_has_new_shape(self, session, git_repo):
        """Result dict has the new structured format."""
        task_id = "TASK-VS"
        make_task(session, task_id, status="completed")
        _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        result = validate_task(session, task_id, str(git_repo))

        assert set(result.keys()) >= {"passed", "branch", "summary", "checks", "checkout_error"}
        for chk in result["checks"]:
            assert "name" in chk
            assert "passed" in chk
            assert "output" in chk
            assert "duration_s" in chk

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
        """Branch with unused import: ruff check fails → task transitions to failed."""
        task_id = "TASK-V03"
        task = make_task(session, task_id, status="completed")
        task.validators = ["ruff"]
        session.flush()
        _make_run(session, task_id)
        _make_agent_branch(
            git_repo,
            task_id,
            extra_files={"bad.py": "import os\n\nx = 1\n"},
        )

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is False
        ruff_check = next(c for c in result["checks"] if c["name"] == "ruff")
        assert ruff_check["passed"] is False
        assert ruff_check["returncode"] != 0

        task = session.get(Task, task_id)
        assert task.status == "failed"

    def test_fails_pytest(self, session, git_repo):
        """Branch with a failing test: pytest fails → task transitions to failed."""
        task_id = "TASK-V04"
        task = make_task(session, task_id, status="completed")
        task.validators = ["pytest"]
        session.flush()
        _make_run(session, task_id)
        _make_agent_branch(
            git_repo,
            task_id,
            extra_files={"test_fail.py": "def test_broken():\n    assert False\n"},
        )

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is False
        pytest_check = next(c for c in result["checks"] if c["name"] == "pytest")
        assert pytest_check["passed"] is False
        assert pytest_check["returncode"] != 0

        task = session.get(Task, task_id)
        assert task.status == "failed"

    def test_transitions_run_result_on_fail(self, session, git_repo):
        """Run.result is set to 'validation_failed' on failure."""
        task_id = "TASK-V05"
        task = make_task(session, task_id, status="completed")
        task.validators = ["pytest"]
        session.flush()
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
        task = make_task(session, task_id, status="completed")
        task.validators = ["ruff"]
        session.flush()
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

    def test_file_exists_fails_when_output_missing(self, session, git_repo):
        """file-exists check fails when a declared output is absent from the branch."""
        task_id = "TASK-VFE"
        task = make_task(session, task_id, status="completed")
        task.outputs = ["expected_output.py"]
        task.validators = ["ruff"]
        session.flush()

        # Branch exists but does NOT write expected_output.py
        _make_agent_branch(git_repo, task_id)
        _make_run(session, task_id)

        result = validate_task(session, task_id, str(git_repo))

        assert result["passed"] is False
        fe_check = next(c for c in result["checks"] if c["name"] == "file-exists")
        assert fe_check["passed"] is False
        assert "expected_output.py" in fe_check["output"]

    def test_backward_compat_empty_validators(self, session, git_repo):
        """Old tasks with validators=[] auto-detect from outputs at validation time."""
        task_id = "TASK-VBC"
        task = make_task(session, task_id, status="completed")
        task.outputs = ["health.py"]
        # validators intentionally left as [] (old task)
        task.validators = []
        session.flush()

        _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        result = validate_task(session, task_id, str(git_repo))

        # Should have auto-detected ruff and pytest from .py output
        check_names = [c["name"] for c in result["checks"]]
        assert "ruff" in check_names
        assert "pytest" in check_names

    def test_assigned_validators_respected(self, session, git_repo):
        """task.validators controls which shell validators run."""
        task_id = "TASK-VAV"
        task = make_task(session, task_id, status="completed")
        task.validators = ["ruff"]  # only ruff, not pytest
        session.flush()

        _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        result = validate_task(session, task_id, str(git_repo))

        check_names = [c["name"] for c in result["checks"]]
        assert "ruff" in check_names
        assert "pytest" not in check_names

    def test_unknown_validator_produces_failed_check(self, session, git_repo):
        """An unknown validator name yields a failed check with a descriptive message."""
        task_id = "TASK-VUK"
        task = make_task(session, task_id, status="completed")
        task.validators = ["nonexistent-validator"]
        session.flush()

        _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        result = validate_task(session, task_id, str(git_repo))

        unknown_check = next(
            (c for c in result["checks"] if c["name"] == "nonexistent-validator"), None
        )
        assert unknown_check is not None
        assert unknown_check["passed"] is False
        assert "not found in registry" in unknown_check["output"]

    def test_llm_acceptance_skipped_when_no_criteria(self, session, git_repo):
        """llm-acceptance check is skipped (passes) when task has no acceptance criteria."""
        task_id = "TASK-VLA"
        task = make_task(session, task_id, status="completed")
        task.acceptance = []
        task.validators = []
        session.flush()

        _make_run(session, task_id)
        _make_agent_branch(git_repo, task_id)

        result = validate_task(session, task_id, str(git_repo))

        llm_check = next(c for c in result["checks"] if c["name"] == "llm-acceptance")
        assert llm_check["passed"] is True
        assert "skipped" in llm_check["output"].lower()

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

        results = validate_task(session, task_id, str(git_repo))
        assert results["passed"] is True
        session.refresh(task)
        assert task.status == "validated"
