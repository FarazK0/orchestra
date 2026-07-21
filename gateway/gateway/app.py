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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Generator

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import AgentMemory
from orchestrator.orchestrator.db import ArtifactProvenance
from orchestrator.orchestrator.db import Event as EventORM
from orchestrator.orchestrator.db import Task as TaskORM
from orchestrator.orchestrator.db import get_engine, get_session_factory

from .audit import write_gateway_audit
from .permissions import (
    PermissionDeniedError,
    check_active_run,
    check_validated_task,
    check_write_scope,
    safe_path,
    verify_capability_header,
)


_metrics_exposed = False


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _metrics_exposed
    if not _metrics_exposed:
        from orchestrator.orchestrator.telemetry import setup_tracing

        setup_tracing(app, "gateway")
        _instrumentator.expose(app)
        _metrics_exposed = True
    yield


app = FastAPI(title="Orchestra Tool Gateway", version="0.1.0", lifespan=_lifespan)
_instrumentator = Instrumentator().instrument(app)

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
    worktree_path: str | None = None


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


class MemoryUpsert(BaseModel):
    task_id: str | None = None
    agent_id: str | None = None  # trusted only when X-Platform-Actor header present
    project_id: str = "default"
    memory_type: str  # "identity" | "episode" | "skill"
    key: str
    content: str


class MemoryUpsertResponse(BaseModel):
    memory_id: str
    agent_id: str


class MemorySearch(BaseModel):
    task_id: str
    query: str
    memory_type: str | None = None  # filter to one type; None = all types
    max_results: int = Field(default=5, ge=1, le=20)


class MemorySearchResult(BaseModel):
    key: str
    memory_type: str
    snippet: str  # first 300 chars of content
    updated_at: str


class MemorySearchResponse(BaseModel):
    results: list[MemorySearchResult]


# Platform actors allowed to bypass the running-task check and write any memory type.
_PLATFORM_ACTORS: frozenset[str] = frozenset({"dispatcher", "root-agent"})

# ---------------------------------------------------------------------------
# Best-effort Redis publish for TASK_DISCOVERED events
# ---------------------------------------------------------------------------

_gw_publisher = None
_gw_publisher_lock = None


def _get_gw_publisher():
    global _gw_publisher
    if _gw_publisher is None:
        try:
            from orchestrator.orchestrator.streams import StreamPublisher, get_redis_url

            _gw_publisher = StreamPublisher(get_redis_url())
        except Exception:
            pass
    return _gw_publisher


def _try_publish_task_discovered(event_id: str, task_id: str, payload: dict) -> None:
    try:
        pub = _get_gw_publisher()
        if pub is not None:
            pub.publish(event_id, "TASK_DISCOVERED", task_id, payload)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to publish TASK_DISCOVERED for task %s: %s", task_id, exc
        )


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
def read_artifact(
    body: ArtifactRead,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> ArtifactReadResponse:
    """Read a file from the managed repo. Audited."""
    try:
        verify_capability_header(authorization)
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

    # Look up stored provenance; fall back to heuristics when no record exists.
    prov_row = session.execute(
        select(ArtifactProvenance).where(
            ArtifactProvenance.repo_path == body.repo_path,
            ArtifactProvenance.file_path == body.path,
        )
    ).scalar_one_or_none()
    if prov_row is not None:
        provenance = prov_row.provenance
    elif body.path.startswith("docs/adr/"):
        provenance = "human"
    else:
        provenance = "agent"

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="read_artifact",
        task_id=body.task_id,
        details={"path": body.path, "found": found, "provenance": provenance},
    )

    return ArtifactReadResponse(path=body.path, content=content, found=found, provenance=provenance)


@app.post("/write_artifact", response_model=ArtifactWriteResponse)
def write_artifact(
    body: ArtifactWrite,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> ArtifactWriteResponse:
    """Write (create or overwrite) a file in the managed repo. Audited."""
    try:
        claims = verify_capability_header(authorization)
        check_active_run(session, body.agent_id, body.task_id)
        check_write_scope(claims, body.path)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)
    try:
        full_path = safe_path(repo, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(body.content, encoding="utf-8")

    # Persist provenance so future readers (context packager, read_artifact) can look it up.
    _now = datetime.now(timezone.utc)
    session.execute(
        pg_insert(ArtifactProvenance)
        .values(
            id=uuid.uuid4(),
            repo_path=body.repo_path,
            file_path=body.path,
            provenance=body.provenance,
            set_by_task=body.task_id,
            set_at=_now,
        )
        .on_conflict_do_update(
            constraint="uq_artifact_provenance",
            set_={"provenance": body.provenance, "set_by_task": body.task_id, "set_at": _now},
        )
    )

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="write_artifact",
        task_id=body.task_id,
        details={"path": body.path, "provenance": body.provenance, "bytes": len(body.content)},
    )

    return ArtifactWriteResponse(path=body.path, written=True)


@app.post("/run_command", response_model=CommandRunResponse)
def run_command(
    body: CommandRun,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> CommandRunResponse:
    """Run a command in the managed repo directory. Audited.

    Phase 1: subprocess with timeout. Docker sandboxing (no-network) is
    deferred to Phase 3; see ADR-005.
    """
    try:
        verify_capability_header(authorization)
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
def emit_event(
    body: EventEmit,
    session: SessionDep,
    bg: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> EventEmitResponse:
    """Write an event to the control plane on behalf of the agent. Audited."""
    try:
        verify_capability_header(authorization)
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

    # Publish TASK_DISCOVERED to Redis stream so the Dispatcher can process it.
    if body.event_type == "TASK_DISCOVERED":
        bg.add_task(
            _try_publish_task_discovered,
            str(event.event_id),
            body.task_id,
            body.payload,
        )

    return EventEmitResponse(event_id=str(event.event_id))


@app.post("/git/branch", response_model=GitBranchResponse)
def git_branch(
    body: GitBranch,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> GitBranchResponse:
    """Create or switch to a branch in the managed repo. Audited."""
    try:
        verify_capability_header(authorization)
        check_active_run(session, body.agent_id, body.task_id)
    except PermissionDeniedError as exc:
        raise _deny(exc)

    repo = Path(body.repo_path)

    # Create an isolated worktree per branch so concurrent agents don't share
    # the same working directory and collide on git checkout.
    worktrees_base = Path("/tmp/orchestra/worktrees")
    worktrees_base.mkdir(parents=True, exist_ok=True)
    branch_slug = body.branch.replace("/", "_")
    worktree_path = worktrees_base / branch_slug

    import shutil

    created = False

    # Check if the worktree is already registered in *this* repo (not a stale
    # leftover from a previous test or process that used a different git repo).
    wt_list = _git(["worktree", "list", "--porcelain"], cwd=repo)
    is_our_worktree = worktree_path.exists() and any(
        line.strip() == f"worktree {worktree_path}"
        for line in (wt_list.stdout.splitlines() if wt_list.returncode == 0 else [])
    )

    if is_our_worktree:
        # Valid worktree already registered; ensure we're on the right branch.
        result = _git(["checkout", body.branch], cwd=worktree_path)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"git worktree checkout failed: {result.stderr.strip()}",
            )
    else:
        # No valid worktree at this path — clean up any stale directory first.
        if worktree_path.exists():
            _git(["worktree", "prune"], cwd=repo)
            shutil.rmtree(worktree_path, ignore_errors=True)

        # Create new worktree + new branch, or attach to an existing branch.
        result = _git(["worktree", "add", str(worktree_path), "-b", body.branch], cwd=repo)
        if result.returncode == 0:
            created = True
        else:
            result = _git(["worktree", "add", str(worktree_path), body.branch], cwd=repo)
            if result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"git worktree add failed: {result.stderr.strip()}",
                )

    write_gateway_audit(
        session,
        actor=body.agent_id,
        operation="git_branch",
        task_id=body.task_id,
        details={"branch": body.branch, "created": created, "worktree_path": str(worktree_path)},
    )

    return GitBranchResponse(branch=body.branch, created=created, worktree_path=str(worktree_path))


@app.post("/git/commit", response_model=GitCommitResponse)
def git_commit(
    body: GitCommit,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> GitCommitResponse:
    """Stage specified paths and commit in the managed repo. Audited."""
    try:
        claims = verify_capability_header(authorization)
        check_active_run(session, body.agent_id, body.task_id)
        for p in body.paths:
            check_write_scope(claims, p)
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

    # Remove the agent worktree now that the branch has been merged.
    branch_slug = body.branch.replace("/", "_")
    worktree_path = Path("/tmp/orchestra/worktrees") / branch_slug
    if worktree_path.exists():
        _git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo)

    return GitMergeResponse(sha=sha, merged=True)


@app.post("/memory/upsert", response_model=MemoryUpsertResponse)
def memory_upsert(
    body: MemoryUpsert,
    session: SessionDep,
    x_platform_actor: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> MemoryUpsertResponse:
    """Write or update an agent memory entry. Audited.

    Platform actors (dispatcher, root-agent) supply X-Platform-Actor header and
    may write any memory_type; agent_id is taken from the body (trusted).
    Regular agent callers must supply task_id of a running task; agent_id is
    derived from tasks.owner; only memory_type='skill' is accepted.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    is_platform = x_platform_actor in _PLATFORM_ACTORS

    if is_platform:
        # Platform writes: agent_id must be supplied in body.
        if not body.agent_id:
            raise HTTPException(status_code=400, detail="agent_id required for platform writes")
        resolved_agent_id = body.agent_id
    else:
        # Agent writes: require capability token + derive agent_id from the running task.
        if not body.task_id:
            raise HTTPException(status_code=400, detail="task_id required")
        try:
            verify_capability_header(authorization)
        except PermissionDeniedError as exc:
            raise _deny(exc)
        try:
            _run, task = check_active_run(session, body.agent_id or "", body.task_id)
        except PermissionDeniedError:
            # Fallback: look up task directly (agent may not have a Run yet).
            task = session.get(TaskORM, body.task_id)
            if task is None or task.status != "running":
                raise HTTPException(
                    status_code=403,
                    detail=f"task {body.task_id!r} is not running or not found",
                )
        resolved_agent_id = task.owner
        if body.memory_type != "skill":
            raise HTTPException(
                status_code=403,
                detail="Agents may only write memory_type='skill'; identity and episode are platform-only",
            )

    if len(body.content) > 2000:
        raise HTTPException(
            status_code=400,
            detail=f"content exceeds 2000-char limit ({len(body.content)} chars)",
        )

    # Skill deduplication: reuse an existing row with the same topic rather than
    # creating a new skill/{topic}/{task_id} row every task.
    effective_key = body.key
    if body.memory_type == "skill" and "/" in body.key:
        # key format: "skill/{topic}/{task_id}" — extract the topic prefix
        parts = body.key.split("/")
        if len(parts) >= 3:
            topic_prefix = f"{parts[0]}/{parts[1]}/"
            existing = (
                session.execute(
                    select(AgentMemory)
                    .where(
                        AgentMemory.agent_id == resolved_agent_id,
                        AgentMemory.project_id == body.project_id,
                        AgentMemory.key.like(f"{topic_prefix}%"),
                    )
                    .order_by(AgentMemory.updated_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if existing:
                effective_key = existing.key  # overwrite the existing row's key

    now = datetime.now(timezone.utc)
    memory_id = _uuid.uuid4()

    stmt = (
        pg_insert(AgentMemory)
        .values(
            id=memory_id,
            agent_id=resolved_agent_id,
            project_id=body.project_id,
            memory_type=body.memory_type,
            key=effective_key,
            content=body.content,
            source_task_id=body.task_id,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_agent_memory",
            set_={
                "content": body.content,
                "source_task_id": body.task_id,
                "updated_at": now,
            },
        )
        .returning(AgentMemory.id)
    )

    result = session.execute(stmt)
    returned_id = result.scalar_one()

    write_gateway_audit(
        session,
        actor=x_platform_actor or resolved_agent_id,
        operation="memory_upsert",
        task_id=body.task_id or "",
        details={
            "agent_id": resolved_agent_id,
            "memory_type": body.memory_type,
            "key": body.key,
            "bytes": len(body.content),
        },
    )

    return MemoryUpsertResponse(memory_id=str(returned_id), agent_id=resolved_agent_id)


@app.post("/memory/search", response_model=MemorySearchResponse)
def memory_search(
    body: MemorySearch,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> MemorySearchResponse:
    """Search agent memories by keyword during task execution. Audited.

    Searches the calling agent's own memories plus the shared project pool
    (agent_id='shared') using Postgres ILIKE. Returns up to max_results matches.
    """
    try:
        verify_capability_header(authorization)
    except PermissionDeniedError as exc:
        raise _deny(exc)
    # Derive agent_id from the running task (same trust model as /memory/upsert).
    try:
        _run, task = check_active_run(session, "", body.task_id)
    except PermissionDeniedError:
        task = session.get(TaskORM, body.task_id)
        if task is None or task.status != "running":
            raise HTTPException(
                status_code=403,
                detail=f"task {body.task_id!r} is not running or not found",
            )
    agent_id = task.owner

    q = (
        select(AgentMemory)
        .where(
            AgentMemory.agent_id.in_([agent_id, "shared"]),
            AgentMemory.project_id == "default",
            AgentMemory.content.ilike(f"%{body.query}%"),
        )
        .order_by(AgentMemory.last_used_at.desc().nulls_last(), AgentMemory.updated_at.desc())
        .limit(body.max_results)
    )
    if body.memory_type:
        q = q.where(AgentMemory.memory_type == body.memory_type)

    rows = session.execute(q).scalars().all()

    write_gateway_audit(
        session,
        actor=agent_id,
        operation="memory_search",
        task_id=body.task_id,
        details={"query": body.query, "hits": len(rows)},
    )

    return MemorySearchResponse(
        results=[
            MemorySearchResult(
                key=r.key,
                memory_type=r.memory_type,
                snippet=r.content[:300],
                updated_at=r.updated_at.isoformat(),
            )
            for r in rows
        ]
    )
