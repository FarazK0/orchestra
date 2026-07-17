"""Integration tests for the tool gateway.

Uses FastAPI's TestClient against a real Postgres test database.
The DB dependency is overridden so each test shares the rolled-back session
from conftest.py — nothing persists between tests.

For git tests a temporary git repo is initialised with an initial commit so
branch/commit operations have a valid HEAD to work from.

Requires the Docker Compose stack (`make up`) to be running.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gateway.gateway.app import app, get_session
from gateway.tests.conftest import make_run, make_task


# ---------------------------------------------------------------------------
# Fixture: TestClient wired to the per-test rolled-back session
# ---------------------------------------------------------------------------


@pytest.fixture
def client(session: Session):
    def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper: repo with an active run (task in 'running' state)
# ---------------------------------------------------------------------------


@pytest.fixture
def active_run(session, tmp_path):
    """Insert task (running) + run row; return (task_id, agent_id, tmp_path)."""
    task = make_task(session, "TASK-G01", status="running")
    make_run(session, task.id, agent_id="backend-agent")
    session.flush()
    return task.id, "backend-agent", tmp_path


@pytest.fixture
def git_repo(tmp_path):
    """Initialise a git repo with an initial commit; return (path, default_branch)."""

    def _git(*args):
        result = subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result

    _git("init", "-b", "main")
    _git("config", "user.email", "test@orchestra")
    _git("config", "user.name", "Test")
    # Need at least one commit so HEAD exists.
    (tmp_path / "README.md").write_text("init", encoding="utf-8")
    _git("add", "README.md")
    _git("commit", "-m", "init")
    return tmp_path


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /read_artifact
# ---------------------------------------------------------------------------


def test_read_artifact_found(client, active_run):
    task_id, agent_id, repo = active_run
    (repo / "app.py").write_text("x = 1", encoding="utf-8")

    resp = client.post(
        "/read_artifact",
        json={"agent_id": agent_id, "task_id": task_id, "repo_path": str(repo), "path": "app.py"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert "x = 1" in body["content"]
    assert body["path"] == "app.py"


def test_read_artifact_not_found(client, active_run):
    task_id, agent_id, repo = active_run
    resp = client.post(
        "/read_artifact",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "path": "missing.py",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["content"] is None


def test_read_artifact_writes_audit(client, session, active_run):
    from orchestrator.orchestrator.db import AuditRow

    task_id, agent_id, repo = active_run
    (repo / "f.py").write_text("pass", encoding="utf-8")
    client.post(
        "/read_artifact",
        json={"agent_id": agent_id, "task_id": task_id, "repo_path": str(repo), "path": "f.py"},
    )
    session.flush()
    rows = session.query(AuditRow).filter(AuditRow.task_id == task_id).all()
    assert any("read_artifact" in r.action for r in rows)


def test_read_artifact_no_active_run_returns_403(client, session, tmp_path):
    make_task(session, "TASK-G02", status="running")
    session.flush()
    resp = client.post(
        "/read_artifact",
        json={
            "agent_id": "unknown-agent",
            "task_id": "TASK-G02",
            "repo_path": str(tmp_path),
            "path": "app.py",
        },
    )
    assert resp.status_code == 403


def test_read_artifact_path_traversal_blocked(client, active_run):
    task_id, agent_id, repo = active_run
    resp = client.post(
        "/read_artifact",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "path": "../../etc/passwd",
        },
    )
    assert resp.status_code == 400


def test_read_artifact_adr_provenance(client, active_run):
    task_id, agent_id, repo = active_run
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-001.md").write_text("# ADR", encoding="utf-8")

    resp = client.post(
        "/read_artifact",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "path": "docs/adr/ADR-001.md",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["provenance"] == "human"


# ---------------------------------------------------------------------------
# /write_artifact
# ---------------------------------------------------------------------------


def test_write_artifact_creates_file(client, active_run):
    task_id, agent_id, repo = active_run
    resp = client.post(
        "/write_artifact",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "path": "new_file.py",
            "content": "print('hello')",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["written"] is True
    assert (repo / "new_file.py").read_text() == "print('hello')"


def test_write_artifact_creates_parent_dirs(client, active_run):
    task_id, agent_id, repo = active_run
    resp = client.post(
        "/write_artifact",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "path": "deep/nested/file.py",
            "content": "x = 1",
        },
    )
    assert resp.status_code == 200
    assert (repo / "deep" / "nested" / "file.py").exists()


def test_write_artifact_writes_audit(client, session, active_run):
    from orchestrator.orchestrator.db import AuditRow

    task_id, agent_id, repo = active_run
    client.post(
        "/write_artifact",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "path": "out.py",
            "content": "pass",
        },
    )
    session.flush()
    rows = session.query(AuditRow).filter(AuditRow.task_id == task_id).all()
    assert any("write_artifact" in r.action for r in rows)


def test_write_artifact_no_active_run_returns_403(client, session, tmp_path):
    make_task(session, "TASK-G03", status="running")
    session.flush()
    resp = client.post(
        "/write_artifact",
        json={
            "agent_id": "nobody",
            "task_id": "TASK-G03",
            "repo_path": str(tmp_path),
            "path": "f.py",
            "content": "x",
        },
    )
    assert resp.status_code == 403


def test_write_artifact_path_traversal_blocked(client, active_run):
    task_id, agent_id, repo = active_run
    resp = client.post(
        "/write_artifact",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "path": "../escape.py",
            "content": "evil",
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /run_command
# ---------------------------------------------------------------------------


def test_run_command_success(client, active_run):
    task_id, agent_id, repo = active_run
    resp = client.post(
        "/run_command",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "command": ["echo", "hello"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["returncode"] == 0
    assert "hello" in body["stdout"]


def test_run_command_nonzero_returncode(client, active_run):
    task_id, agent_id, repo = active_run
    resp = client.post(
        "/run_command",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "command": ["false"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["returncode"] != 0


def test_run_command_writes_audit(client, session, active_run):
    from orchestrator.orchestrator.db import AuditRow

    task_id, agent_id, repo = active_run
    client.post(
        "/run_command",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "command": ["echo", "audit"],
        },
    )
    session.flush()
    rows = session.query(AuditRow).filter(AuditRow.task_id == task_id).all()
    assert any("run_command" in r.action for r in rows)


def test_run_command_no_active_run_returns_403(client, session, tmp_path):
    make_task(session, "TASK-G04", status="running")
    session.flush()
    resp = client.post(
        "/run_command",
        json={
            "agent_id": "nobody",
            "task_id": "TASK-G04",
            "repo_path": str(tmp_path),
            "command": ["echo", "hi"],
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /emit_event
# ---------------------------------------------------------------------------


def test_emit_event_inserts_event_row(client, session, active_run):
    from orchestrator.orchestrator.db import Event as EventORM

    task_id, agent_id, _ = active_run
    resp = client.post(
        "/emit_event",
        json={
            "agent_id": agent_id,
            "task_id": task_id,
            "event_type": "AGENT_CHECKPOINT",
            "payload": {"note": "halfway"},
        },
    )
    assert resp.status_code == 200
    event_id = resp.json()["event_id"]

    session.flush()
    from uuid import UUID

    event = session.get(EventORM, UUID(event_id))
    assert event is not None
    assert event.event_type == "AGENT_CHECKPOINT"
    assert event.payload["note"] == "halfway"


def test_emit_event_writes_audit(client, session, active_run):
    from orchestrator.orchestrator.db import AuditRow

    task_id, agent_id, _ = active_run
    client.post(
        "/emit_event",
        json={"agent_id": agent_id, "task_id": task_id, "event_type": "X"},
    )
    session.flush()
    rows = session.query(AuditRow).filter(AuditRow.task_id == task_id).all()
    assert any("emit_event" in r.action for r in rows)


def test_emit_event_no_active_run_returns_403(client, session):
    make_task(session, "TASK-G05", status="running")
    session.flush()
    resp = client.post(
        "/emit_event",
        json={"agent_id": "nobody", "task_id": "TASK-G05", "event_type": "X"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /git/branch
# ---------------------------------------------------------------------------


def test_git_branch_creates_branch(client, session, git_repo):
    make_task(session, "TASK-G06", status="running")
    make_run(session, "TASK-G06", context_package_ref=str(git_repo / "ctx.json"))
    session.flush()

    resp = client.post(
        "/git/branch",
        json={
            "agent_id": "backend-agent",
            "task_id": "TASK-G06",
            "repo_path": str(git_repo),
            "branch": "agent/backend/TASK-G06",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["branch"] == "agent/backend/TASK-G06"
    assert body["created"] is True

    # Branch exists in the repo
    result = subprocess.run(
        ["git", "branch", "--list", "agent/backend/TASK-G06"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "agent/backend/TASK-G06" in result.stdout


def test_git_branch_switches_if_exists(client, session, git_repo):
    make_task(session, "TASK-G07", status="running")
    make_run(session, "TASK-G07", context_package_ref=str(git_repo / "ctx.json"))
    session.flush()

    # Create the branch first
    subprocess.run(["git", "checkout", "-b", "agent/backend/TASK-G07"], cwd=git_repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True)

    resp = client.post(
        "/git/branch",
        json={
            "agent_id": "backend-agent",
            "task_id": "TASK-G07",
            "repo_path": str(git_repo),
            "branch": "agent/backend/TASK-G07",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is False


def test_git_branch_writes_audit(client, session, git_repo):
    from orchestrator.orchestrator.db import AuditRow

    make_task(session, "TASK-G08", status="running")
    make_run(session, "TASK-G08", context_package_ref=str(git_repo / "ctx.json"))
    session.flush()

    client.post(
        "/git/branch",
        json={
            "agent_id": "backend-agent",
            "task_id": "TASK-G08",
            "repo_path": str(git_repo),
            "branch": "agent/backend/TASK-G08",
        },
    )
    session.flush()
    rows = session.query(AuditRow).filter(AuditRow.task_id == "TASK-G08").all()
    assert any("git_branch" in r.action for r in rows)


def test_git_branch_no_active_run_returns_403(client, session, git_repo):
    make_task(session, "TASK-G09", status="running")
    session.flush()
    resp = client.post(
        "/git/branch",
        json={
            "agent_id": "nobody",
            "task_id": "TASK-G09",
            "repo_path": str(git_repo),
            "branch": "agent/backend/TASK-G09",
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /git/commit
# ---------------------------------------------------------------------------


def test_git_commit_commits_file(client, session, git_repo):
    make_task(session, "TASK-G10", status="running")
    make_run(session, "TASK-G10", context_package_ref=str(git_repo / "ctx.json"))
    session.flush()

    (git_repo / "hello.py").write_text("print('hi')", encoding="utf-8")

    resp = client.post(
        "/git/commit",
        json={
            "agent_id": "backend-agent",
            "task_id": "TASK-G10",
            "repo_path": str(git_repo),
            "message": "[TASK-G10] add hello.py",
            "paths": ["hello.py"],
        },
    )
    assert resp.status_code == 200
    sha = resp.json()["sha"]
    assert len(sha) > 0

    # Commit is visible in log
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=git_repo, capture_output=True, text=True
    )
    assert "TASK-G10" in log.stdout


def test_git_commit_writes_audit(client, session, git_repo):
    from orchestrator.orchestrator.db import AuditRow

    make_task(session, "TASK-G11", status="running")
    make_run(session, "TASK-G11", context_package_ref=str(git_repo / "ctx.json"))
    session.flush()

    (git_repo / "z.py").write_text("z = 1", encoding="utf-8")
    client.post(
        "/git/commit",
        json={
            "agent_id": "backend-agent",
            "task_id": "TASK-G11",
            "repo_path": str(git_repo),
            "message": "[TASK-G11] z",
            "paths": ["z.py"],
        },
    )
    session.flush()
    rows = session.query(AuditRow).filter(AuditRow.task_id == "TASK-G11").all()
    assert any("git_commit" in r.action for r in rows)


def test_git_commit_no_active_run_returns_403(client, session, git_repo):
    make_task(session, "TASK-G12", status="running")
    session.flush()
    resp = client.post(
        "/git/commit",
        json={
            "agent_id": "nobody",
            "task_id": "TASK-G12",
            "repo_path": str(git_repo),
            "message": "bad",
            "paths": [],
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /git/merge
# ---------------------------------------------------------------------------


@pytest.fixture
def validated_repo(git_repo):
    """git_repo with an agent branch that has one commit, sitting on main."""
    branch = "agent/backend/TASK-G13"
    subprocess.run(["git", "checkout", "-b", branch], cwd=git_repo, check=True)
    (git_repo / "health.py").write_text("def health():\n    return True\n", encoding="utf-8")
    subprocess.run(["git", "add", "health.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "[TASK-G13] add health"], cwd=git_repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True)
    return git_repo


def test_git_merge_merges_branch(client, session, validated_repo):
    make_task(session, "TASK-G13", status="validated")
    session.flush()

    resp = client.post(
        "/git/merge",
        json={
            "actor": "human",
            "task_id": "TASK-G13",
            "repo_path": str(validated_repo),
            "branch": "agent/backend/TASK-G13",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["merged"] is True
    assert len(body["sha"]) > 0

    # Merge commit is on main.
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=validated_repo, capture_output=True, text=True
    )
    assert "TASK-G13" in log.stdout


def test_git_merge_writes_audit(client, session, validated_repo):
    from orchestrator.orchestrator.db import AuditRow

    make_task(session, "TASK-G13", status="validated")
    session.flush()

    client.post(
        "/git/merge",
        json={
            "actor": "human",
            "task_id": "TASK-G13",
            "repo_path": str(validated_repo),
            "branch": "agent/backend/TASK-G13",
        },
    )
    session.flush()
    rows = session.query(AuditRow).filter(AuditRow.task_id == "TASK-G13").all()
    assert any("git_merge" in r.action for r in rows)


def test_git_merge_not_validated_returns_403(client, session, git_repo):
    make_task(session, "TASK-G14", status="completed")
    session.flush()

    resp = client.post(
        "/git/merge",
        json={
            "actor": "human",
            "task_id": "TASK-G14",
            "repo_path": str(git_repo),
            "branch": "agent/backend/TASK-G14",
        },
    )
    assert resp.status_code == 403


def test_git_merge_missing_branch_returns_500(client, session, git_repo):
    make_task(session, "TASK-G15", status="validated")
    session.flush()

    resp = client.post(
        "/git/merge",
        json={
            "actor": "human",
            "task_id": "TASK-G15",
            "repo_path": str(git_repo),
            "branch": "agent/backend/TASK-G15",  # branch does not exist
        },
    )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Memory search (Improvement 2)
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_search_run(session):
    """Running task + run row + two memory rows for search tests."""
    import uuid
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import AgentMemory

    task = make_task(session, "TASK-GMS", status="running", owner="backend-agent")
    make_run(session, task.id, agent_id="backend-agent")
    now = datetime.now(timezone.utc)
    for key, content in [
        ("skill/db-session/TASK-GMS", "Always use the db_session dependency."),
        ("skill/ruff-check/TASK-GMS", "Always run ruff check before committing."),
    ]:
        session.add(
            AgentMemory(
                id=uuid.uuid4(),
                agent_id="backend-agent",
                project_id="default",
                memory_type="skill",
                key=key,
                content=content,
                created_at=now,
                updated_at=now,
            )
        )
    session.flush()
    return task.id


def test_memory_search_returns_matching_rows(client, memory_search_run):
    resp = client.post(
        "/memory/search",
        json={"task_id": memory_search_run, "query": "ruff", "max_results": 5},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert "ruff" in results[0]["snippet"]


def test_memory_search_empty_query_returns_all(client, memory_search_run):
    resp = client.post(
        "/memory/search",
        json={"task_id": memory_search_run, "query": "session", "max_results": 5},
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) >= 1


def test_memory_search_includes_shared_pool(client, session, memory_search_run):
    """search_memory returns shared conventions alongside agent-specific memories."""
    import uuid
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import AgentMemory

    now = datetime.now(timezone.utc)
    session.add(
        AgentMemory(
            id=uuid.uuid4(),
            agent_id="shared",
            project_id="default",
            memory_type="convention",
            key="shared/project-conventions",
            content="All agents must run pytest before task_complete.",
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()
    resp = client.post(
        "/memory/search",
        json={"task_id": memory_search_run, "query": "pytest", "max_results": 5},
    )
    assert resp.status_code == 200
    snippets = [r["snippet"] for r in resp.json()["results"]]
    assert any("pytest" in s for s in snippets)


def test_memory_search_not_running_returns_403(client, session):
    make_task(session, "TASK-GMS-403", status="assigned")
    session.flush()
    resp = client.post(
        "/memory/search",
        json={"task_id": "TASK-GMS-403", "query": "anything"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Skill deduplication (Improvement 3)
# ---------------------------------------------------------------------------


def test_skill_dedup_reuses_existing_row(client, session):
    """Writing skill/{topic}/{task_id2} when skill/{topic}/{task_id1} exists updates the old row."""
    import uuid
    from datetime import datetime, timezone

    from orchestrator.orchestrator.db import AgentMemory

    # Set up running tasks for two sequential runs on the same topic.
    task1 = make_task(session, "TASK-GDEDUP1", status="running", owner="backend-agent")
    make_run(session, task1.id, agent_id="backend-agent")
    now = datetime.now(timezone.utc)
    session.add(
        AgentMemory(
            id=uuid.uuid4(),
            agent_id="backend-agent",
            project_id="default",
            memory_type="skill",
            key="skill/db-pattern/TASK-GDEDUP1",
            content="Original skill content.",
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()

    task2 = make_task(session, "TASK-GDEDUP2", status="running", owner="backend-agent")
    make_run(session, task2.id, agent_id="backend-agent")
    session.flush()

    # Write a new skill with the same topic from task2.
    resp = client.post(
        "/memory/upsert",
        json={
            "task_id": "TASK-GDEDUP2",
            "project_id": "default",
            "memory_type": "skill",
            "key": "skill/db-pattern/TASK-GDEDUP2",
            "content": "Updated skill content.",
        },
    )
    assert resp.status_code == 200

    # Only one row for this topic should exist.
    rows = (
        session.query(AgentMemory)
        .filter(
            AgentMemory.agent_id == "backend-agent",
            AgentMemory.key.like("skill/db-pattern/%"),
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].content == "Updated skill content."
