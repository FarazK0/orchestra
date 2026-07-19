"""Tests for the context packager.

Verifies that build_context_package produces the correct structure and that
create_run persists the Run row and writes the package to disk.

Requires the Docker Compose stack to be running: `make up`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.orchestrator.context_packager import (
    TaskNotFoundError,
    build_context_package,
    create_run,
)
from orchestrator.tests.conftest import make_task


# ---------------------------------------------------------------------------
# build_context_package
# ---------------------------------------------------------------------------


def test_build_returns_required_keys(session, tmp_path):
    make_task(session, "TASK-701", status="assigned")
    session.flush()

    pkg = build_context_package(session, "TASK-701", tmp_path)

    assert pkg["schema_version"] == 1
    assert pkg["task_id"] == "TASK-701"
    assert "packaged_at" in pkg
    assert "task" in pkg
    assert "input_artifacts" in pkg
    assert "adrs" in pkg
    assert "agent_instructions" in pkg


def test_build_task_fields(session, tmp_path):
    make_task(
        session,
        "TASK-702",
        title="Add health endpoint",
        status="assigned",
        owner="backend-agent",
    )
    session.flush()

    pkg = build_context_package(session, "TASK-702", tmp_path)
    task_block = pkg["task"]

    assert task_block["id"] == "TASK-702"
    assert task_block["title"] == "Add health endpoint"
    assert task_block["owner"] == "backend-agent"
    assert task_block["status"] == "assigned"


def test_build_agent_instructions(session, tmp_path):
    make_task(session, "TASK-703", status="assigned")
    session.flush()

    pkg = build_context_package(session, "TASK-703", tmp_path)
    instr = pkg["agent_instructions"]

    assert instr["branch"] == "agent/backend/TASK-703"
    assert instr["commit_prefix"] == "[TASK-703]"
    assert "read_scope" in instr
    assert "write_scope" in instr
    assert "acceptance_criteria" in instr


def test_build_reads_input_artifacts(session, tmp_path):
    # Write a file the task lists as an input
    (tmp_path / "app.py").write_text("print('hello')", encoding="utf-8")

    from orchestrator.orchestrator.db import Task

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    task = Task(
        id="TASK-704",
        schema_version=1,
        title="Read file task",
        owner="test",
        status="assigned",
        depends_on=[],
        inputs=["app.py"],
        outputs=[],
        acceptance=["file is read"],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()

    pkg = build_context_package(session, "TASK-704", tmp_path)

    assert len(pkg["input_artifacts"]) == 1
    art = pkg["input_artifacts"][0]
    assert art["path"] == "app.py"
    assert art["found"] is True
    assert "print('hello')" in art["content"]


def test_build_marks_missing_artifacts(session, tmp_path):
    from orchestrator.orchestrator.db import Task

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    task = Task(
        id="TASK-705",
        schema_version=1,
        title="Missing file task",
        owner="test",
        status="assigned",
        depends_on=[],
        inputs=["does_not_exist.py"],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()

    pkg = build_context_package(session, "TASK-705", tmp_path)

    art = pkg["input_artifacts"][0]
    assert art["found"] is False
    assert art["content"] is None


def test_build_reads_adrs(session, tmp_path):
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-001-foo.md").write_text("# ADR-001\nFoo decision.", encoding="utf-8")
    (adr_dir / "ADR-002-bar.md").write_text("# ADR-002\nBar decision.", encoding="utf-8")

    make_task(session, "TASK-706", status="assigned")
    session.flush()

    pkg = build_context_package(session, "TASK-706", tmp_path)

    assert len(pkg["adrs"]) == 2
    paths = [a["path"] for a in pkg["adrs"]]
    assert "docs/adr/ADR-001-foo.md" in paths
    assert "docs/adr/ADR-002-bar.md" in paths


def test_build_no_adrs_when_dir_absent(session, tmp_path):
    make_task(session, "TASK-707", status="assigned")
    session.flush()

    pkg = build_context_package(session, "TASK-707", tmp_path)
    assert pkg["adrs"] == []


def test_build_raises_for_unknown_task(session, tmp_path):
    with pytest.raises(TaskNotFoundError):
        build_context_package(session, "TASK-999", tmp_path)


# ---------------------------------------------------------------------------
# create_run
# ---------------------------------------------------------------------------


def test_create_run_inserts_row(session, tmp_path):
    from orchestrator.orchestrator.db import Run

    make_task(session, "TASK-801", status="assigned")
    session.flush()

    run = create_run(session, "TASK-801", "backend-agent", tmp_path, tmp_path / "store")

    assert run.task_id == "TASK-801"
    assert run.agent_id == "backend-agent"
    assert run.branch == "agent/backend/TASK-801"

    fetched = session.get(Run, run.run_id)
    assert fetched is not None
    assert fetched.context_package_ref == str(tmp_path / "store" / f"{run.run_id}.json")


def test_create_run_writes_json_file(session, tmp_path):
    make_task(session, "TASK-802", status="assigned")
    session.flush()

    store = tmp_path / "context"
    run = create_run(session, "TASK-802", "backend-agent", tmp_path, store)

    package_path = Path(run.context_package_ref)
    assert package_path.exists()

    pkg = json.loads(package_path.read_text(encoding="utf-8"))
    assert pkg["task_id"] == "TASK-802"
    assert pkg["run_id"] == str(run.run_id)
    assert pkg["schema_version"] == 1
    assert pkg["agent_instructions"]["agent_id"] == "backend-agent"


def test_create_run_creates_store_dir(session, tmp_path):
    make_task(session, "TASK-803", status="assigned")
    session.flush()

    store = tmp_path / "deep" / "nested" / "store"
    assert not store.exists()

    create_run(session, "TASK-803", "backend-agent", tmp_path, store)
    assert store.is_dir()


def test_create_run_raises_for_unknown_task(session, tmp_path):
    with pytest.raises(TaskNotFoundError):
        create_run(session, "TASK-999", "backend-agent", tmp_path, tmp_path / "store")


# ---------------------------------------------------------------------------
# Resumption context (Stage 3)
# ---------------------------------------------------------------------------


def test_create_run_child_token_narrowed_to_parent_scope(session, tmp_path):
    """Child task capability token write_scope must be ⊆ parent write_scope."""
    import os
    from unittest.mock import patch

    import jwt
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import Task

    _SECRET = "test-narrow-secret-stage4"
    now = datetime.now(timezone.utc)

    parent = Task(
        id="TASK-850",
        schema_version=1,
        title="Parent task",
        owner="backend-agent",
        status="running",
        depends_on=[],
        inputs=[],
        outputs=["app/"],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        created_at=now,
        updated_at=now,
    )
    session.add(parent)

    child = Task(
        id="TASK-851",
        schema_version=1,
        title="Child task",
        owner="backend-agent",
        status="assigned",
        depends_on=[],
        inputs=[],
        outputs=["app/auth.py", "outside/secret.py"],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        parent_task_id="TASK-850",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(child)
    session.flush()

    with patch.dict(os.environ, {"CAPABILITY_SECRET": _SECRET}):
        run = create_run(session, "TASK-851", "backend-agent", tmp_path, tmp_path / "store")

    import json
    pkg = json.loads((tmp_path / "store" / f"{run.run_id}.json").read_text())
    token = pkg["capability_token"]
    assert token != ""

    claims = jwt.decode(token, _SECRET, algorithms=["HS256"])
    # Token write_scope is narrowed: only "app/auth.py" survives, "outside/secret.py" is dropped
    assert claims["write_scope"] == ["app/auth.py"]


def test_build_resumption_fields_on_first_run(session, tmp_path):
    make_task(session, "TASK-810", status="assigned")
    session.flush()

    pkg = build_context_package(session, "TASK-810", tmp_path)

    assert pkg["is_resumption"] is False
    assert pkg["checkpoint"] is None
    assert pkg["child_outputs"] == []


def test_build_resumption_fields_on_resumed_task(session, tmp_path):
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import Task

    now = datetime.now(timezone.utc)
    checkpoint = {"summary": "done step 1", "completed_steps": ["step 1"], "next_step": "step 2"}
    parent = Task(
        id="TASK-820",
        schema_version=1,
        title="Parent task",
        owner="backend-agent",
        status="assigned",
        depends_on=[],
        inputs=[],
        outputs=[],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        checkpoint=checkpoint,
        created_at=now,
        updated_at=now,
    )
    session.add(parent)

    child = Task(
        id="TASK-821",
        schema_version=1,
        title="Child migration task",
        owner="backend-agent",
        status="completed",
        depends_on=[],
        inputs=[],
        outputs=["db/migration.sql"],
        acceptance=[],
        risk_tier=1,
        budget={"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        parent_task_id="TASK-820",
        spawn_depth=1,
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )
    session.add(child)
    session.flush()

    pkg = build_context_package(session, "TASK-820", tmp_path)

    assert pkg["is_resumption"] is True
    assert pkg["checkpoint"] == checkpoint
    assert len(pkg["child_outputs"]) == 1
    co = pkg["child_outputs"][0]
    assert co["task_id"] == "TASK-821"
    assert co["title"] == "Child migration task"
    assert co["status"] == "completed"
    assert "db/migration.sql" in co["outputs"]
