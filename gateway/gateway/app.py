"""Tool gateway — the ONLY path to side effects for agents.

Endpoints
---------
GET  /healthz
POST /read_artifact     read a file from the managed repo
POST /write_artifact    write a file to the managed repo
POST /run_command       run a command (subprocess, Phase 1; Docker in Phase 3)
POST /emit_event        write an event to the control plane
POST /git/branch        create or checkout a branch
POST /git/commit        stage files and commit

Every non-health endpoint:
  1. Verifies the (agent_id, task_id) allowlist (active run + running task).
  2. Executes the operation.
  3. Writes Event + AuditRow atomically with the operation.

Port: 8081
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Generator

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import Event as EventORM
from orchestrator.orchestrator.db import get_engine, get_session_factory

from .audit import write_gateway_audit
from .permissions import PermissionDeniedError, check_active_run, check_validated_task, safe_path

app = FastAPI(title="Orchestra Tool Gateway", version="0.1.0")

# ---------------------------------------------------------------------------
# DB session dependency (same pattern as orchestrator/api.py)
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
# Request / response bodies
# ---------------------------------------------------------------------------


class ArtifactRead(BaseModel):
    agent_id: str
    task_id: str
    repo_path: str
    path: str


class ArtifactReadResponse(BaseModel):
    path: str
    content: str | None
    found: bool
    provenance: str


class ArtifactWrite(BaseModel):
    agent_id: str
    task_id: str
    repo_path: str
    path: str
    content: str
    provenance: str = "agent"


class ArtifactWriteResponse(BaseModel):
    path: str
    written: bool


class CommandRun(BaseModel):
    agent_id: str
    task_id: str
    repo_path: str
    command: list[str]
    timeout_sec: int = Field(default=30, ge=1, le=300)


class CommandRunResponse(BaseModel):
    returncode: int
    stdout: str
    stderr: str


class EventEmit(BaseModel):
    agent_id: str
    task_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventEmitResponse(BaseModel):
    event_id: str


class GitBranch(BaseModel):
    agent_id: str
    task_id: str
    repo_path: str
    branch: str


class GitBranchResponse(BaseModel):
    branch: str
    created: bool


class GitCommit(BaseModel):
    agent_id: str
    task_id: str
    repo_path: str
    message: str
    paths: list[str]


class GitCommitResponse(BaseModel):
    sha: str


class GitMerge(BaseModel):
    actor: str
    task_id: str
    repo_path: str
    branch: str
    target_branch: str = "main"


class GitMergeResponse(BaseModel):
    sha: str
    merged: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deny(exc: PermissionDeniedError) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


def _git(args: list[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/read_artifact", response_model=ArtifactReadResponse)
def read_artifact(body: ArtifactRead, session: SessionDep) -> ArtifactReadResponse:
    """Read a file from the managed repo. Audited."""
    try:
        _run, task = check_active_run(session, body.agent_id, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)
    try:
        full_path = safe_path(repo, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        content = full_path.read_text(encoding="utf-8")
        found = True
    except (FileNotFoundError, IsADirectoryError):
        content = None
        found = False

    # Infer provenance: human-authored ADRs are human; everything else is agent.
    provenance = "human" if body.path.startswith("docs/adr/") else "agent"

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="read_artifact",
        task_id=body.task_id,
        details={"path": body.path, "found": found},
    )

    return ArtifactReadResponse(path=body.path, content=content, found=found, provenance=provenance)


@app.post("/write_artifact", response_model=ArtifactWriteResponse)
def write_artifact(body: ArtifactWrite, session: SessionDep) -> ArtifactWriteResponse:
    """Write (create or overwrite) a file in the managed repo. Audited."""
    try:
        check_active_run(session, body.agent_id, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)
    try:
        full_path = safe_path(repo, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(body.content, encoding="utf-8")

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="write_artifact",
        task_id=body.task_id,
        details={"path": body.path, "provenance": body.provenance, "bytes": len(body.content)},
    )

    return ArtifactWriteResponse(path=body.path, written=True)


@app.post("/run_command", response_model=CommandRunResponse)
def run_command(body: CommandRun, session: SessionDep) -> CommandRunResponse:
    """Run a command in the managed repo directory. Audited.

    Phase 1: subprocess with timeout. Docker sandboxing (no-network) is
    deferred to Phase 3; see ADR-005.
    """
    try:
        check_active_run(session, body.agent_id, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)
    if not repo.is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path {str(repo)!r} is not a directory")

    try:
        result = subprocess.run(
            body.command,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=body.timeout_sec,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"Command timed out after {body.timeout_sec}s",
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"Command not found: {body.command[0]!r}",
        )

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="run_command",
        task_id=body.task_id,
        details={"command": body.command, "returncode": result.returncode},
    )

    return CommandRunResponse(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


@app.post("/emit_event", response_model=EventEmitResponse)
def emit_event(body: EventEmit, session: SessionDep) -> EventEmitResponse:
    """Write an event to the control plane on behalf of the agent. Audited."""
    try:
        check_active_run(session, body.agent_id, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    now = datetime.now(timezone.utc)
    event = EventORM(
        event_id=uuid.uuid4(),
        schema_version=1,
        event_type=body.event_type,
        task_id=body.task_id,
        emitted_by=body.agent_id,
        emitted_at=now,
        payload=body.payload,
    )
    session.add(event)
    session.flush()

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="emit_event",
        task_id=body.task_id,
        details={"event_type": body.event_type, "event_id": str(event.event_id)},
    )

    return EventEmitResponse(event_id=str(event.event_id))


@app.post("/git/branch", response_model=GitBranchResponse)
def git_branch(body: GitBranch, session: SessionDep) -> GitBranchResponse:
    """Create or switch to a branch in the managed repo. Audited."""
    try:
        check_active_run(session, body.agent_id, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)

    # Try to create; fall back to checkout if it already exists.
    result = _git(["checkout", "-b", body.branch], cwd=repo)
    created = result.returncode == 0
    if not created:
        result = _git(["checkout", body.branch], cwd=repo)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"git branch failed: {result.stderr.strip()}",
            )

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="git_branch",
        task_id=body.task_id,
        details={"branch": body.branch, "created": created},
    )

    return GitBranchResponse(branch=body.branch, created=created)


@app.post("/git/commit", response_model=GitCommitResponse)
def git_commit(body: GitCommit, session: SessionDep) -> GitCommitResponse:
    """Stage specified paths and commit in the managed repo. Audited."""
    try:
        check_active_run(session, body.agent_id, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)

    # Stage the requested paths.
    add_result = _git(["add", "--", *body.paths], cwd=repo)
    if add_result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git add failed: {add_result.stderr.strip()}",
        )

    commit_result = _git(
        ["commit", "-m", body.message, "--allow-empty"],
        cwd=repo,
    )
    if commit_result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git commit failed: {commit_result.stderr.strip()}",
        )

    # Extract the short SHA from the commit output.
    sha_result = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "unknown"

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="git_commit",
        task_id=body.task_id,
        details={"message": body.message, "paths": body.paths, "sha": sha},
    )

    return GitCommitResponse(sha=sha)


@app.post("/git/merge", response_model=GitMergeResponse)
def git_merge(body: GitMerge, session: SessionDep) -> GitMergeResponse:
    """Merge an agent branch into the target branch (default: main). Audited.

    Permission: task must be in 'validated' status (human merge gate).
    """
    try:
        check_validated_task(session, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)

    # Clean the working tree before switching branches.
    # Validation (ruff/pytest) leaves modified tracked files (.pyc) and untracked
    # artifacts that would block the checkout. Reset and clean them first.
    _git(["checkout", "--", "."], cwd=repo)
    _git(["clean", "-fd"], cwd=repo)

    # Switch to the target branch before merging.
    checkout = _git(["checkout", body.target_branch], cwd=repo)
    if checkout.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git checkout {body.target_branch!r} failed: {checkout.stderr.strip()}",
        )

    merge_msg = f"Merge {body.branch} into {body.target_branch} [{body.task_id}]"
    result = _git(["merge", "--no-ff", body.branch, "-m", merge_msg], cwd=repo)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git merge failed: {result.stderr.strip()}",
        )

    sha_result = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "unknown"

    write_gateway_audit(
        session,
        actor=body.actor,
        operation="git_merge",
        task_id=body.task_id,
        details={"branch": body.branch, "target_branch": body.target_branch, "sha": sha},
    )

    return GitMergeResponse(sha=sha, merged=True)
