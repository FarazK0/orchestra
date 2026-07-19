"""Context packager: assembles and persists the context package for an agent run.

For each task the packager:
1. Reads the task spec and acceptance criteria from Postgres.
2. Reads the content of every file listed in task.inputs from the managed repo.
3. Reads all ADRs from docs/adr/ in the managed repo.
4. Serialises the package to {store_dir}/{run_id}.json on disk.
5. Inserts a Run row in Postgres pointing to that file.

The context package is both the agent's read scope and its briefing document --
the exact same dict is stored on disk and handed to the agent, so a run is
fully reproducible from its context_package_ref alone.

Callers own the transaction; this module never commits.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .dag import TERMINAL_STATUSES
from .db import AgentMemory, ArtifactProvenance, Run, Task
from .token import CapabilityError, mint_child_capability_token, mint_token

log = logging.getLogger(__name__)

_MEMORY_WARN_EVERY = 5_000  # chars; warn at every multiple of this

# Maximum number of memories injected per type to prevent context explosion.
# Rows are ranked by last_used_at DESC so the most-recently-seen entries win.
# The full archive remains searchable via POST /memory/search during the task.
_MEMORY_LIMITS: dict[str, int] = {
    "identity": 1,
    "episode": 10,
    "skill": 15,
    "convention": 10,  # shared project-level conventions (agent_id="shared")
}


class TaskNotFoundError(Exception):
    pass


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return None


def build_context_package(
    session: Session,
    task_id: str,
    repo_path: Path,
) -> dict:
    """Assemble the context package dict for *task_id*.

    Reads task spec from Postgres and artifact contents from *repo_path*
    on disk. Returns a plain dict; does not touch the DB or the filesystem.

    Args:
        session:   An open SQLAlchemy Session.
        task_id:   The task to package.
        repo_path: Absolute path to the root of the managed Git repo.

    Raises:
        TaskNotFoundError: task_id is not in the tasks table.
    """
    task = session.get(Task, task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id!r} not found")

    repo_path_str = str(repo_path)

    def _lookup_provenance(rel_path: str) -> str:
        """Return stored provenance for rel_path, falling back to heuristics."""
        row = session.execute(
            select(ArtifactProvenance).where(
                ArtifactProvenance.repo_path == repo_path_str,
                ArtifactProvenance.file_path == rel_path,
            )
        ).scalar_one_or_none()
        if row is not None:
            return row.provenance
        if rel_path.startswith("docs/adr/"):
            return "human"
        return "agent"

    # Input artifacts listed in the task spec
    input_artifacts: list[dict] = []
    for rel_path in task.inputs:
        content = _read_file(repo_path / rel_path)
        provenance = _lookup_provenance(rel_path)
        input_artifacts.append(
            {
                "path": rel_path,
                "content": content,
                "found": content is not None,
                "provenance": provenance,
            }
        )

    # ADRs from docs/adr/ -- always human-provenance decision records
    adr_dir = repo_path / "docs" / "adr"
    adrs: list[dict] = []
    if adr_dir.is_dir():
        for adr_file in sorted(adr_dir.glob("*.md")):
            content = _read_file(adr_file)
            if content is not None:
                adrs.append(
                    {
                        "path": str(adr_file.relative_to(repo_path)),
                        "content": content,
                        "provenance": "human",
                    }
                )

    # Agent memory: top-K per type ordered by recency, plus shared project conventions.
    # Separate queries per type so each gets its own LIMIT without cross-type interference.
    def _fetch_top_k(agent_ids: list[str], mem_type: str, limit: int) -> list[AgentMemory]:
        return (
            session.execute(
                select(AgentMemory)
                .where(
                    AgentMemory.agent_id.in_(agent_ids),
                    AgentMemory.project_id == "default",
                    AgentMemory.memory_type == mem_type,
                )
                .order_by(
                    AgentMemory.last_used_at.desc().nulls_last(), AgentMemory.updated_at.desc()
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

    own_ids = [task.owner]
    identity_rows = _fetch_top_k(own_ids, "identity", _MEMORY_LIMITS["identity"])
    episode_rows = _fetch_top_k(own_ids, "episode", _MEMORY_LIMITS["episode"])
    skill_rows = _fetch_top_k(own_ids, "skill", _MEMORY_LIMITS["skill"])
    shared_rows = _fetch_top_k(["shared"], "convention", _MEMORY_LIMITS["convention"])

    all_injected = identity_rows + episode_rows + skill_rows + shared_rows
    agent_memory: dict | None = None

    if all_injected:
        identity = identity_rows[0].content if identity_rows else None
        episodes = [m.content for m in episode_rows]
        skills = [m.content for m in skill_rows]
        shared_skills = [m.content for m in shared_rows]
        am: dict = {
            "identity": identity,
            "episodes": episodes,
            "skills": skills,
            "shared_skills": shared_skills,
        }

        # Size warning
        total_chars = sum(len(c) for c in [identity or "", *episodes, *skills, *shared_skills])
        warn_level = total_chars // _MEMORY_WARN_EVERY
        warnings: list[str] = []
        if warn_level > 0:
            warnings.append(f"Agent memory is {total_chars} chars (~{total_chars // 4} tokens).")

        # Cap-hit warnings (archive is larger than what's injected)
        total_episode_count = (
            session.query(AgentMemory)
            .filter(
                AgentMemory.agent_id == task.owner,
                AgentMemory.project_id == "default",
                AgentMemory.memory_type == "episode",
            )
            .count()
        )
        total_skill_count = (
            session.query(AgentMemory)
            .filter(
                AgentMemory.agent_id == task.owner,
                AgentMemory.project_id == "default",
                AgentMemory.memory_type == "skill",
            )
            .count()
        )

        if total_episode_count > _MEMORY_LIMITS["episode"]:
            warnings.append(
                f"Showing {len(episodes)} of {total_episode_count} episodes "
                f"(use search_memory tool to query the archive)."
            )
        if total_skill_count > _MEMORY_LIMITS["skill"]:
            warnings.append(
                f"Showing {len(skills)} of {total_skill_count} skills "
                f"(use search_memory tool to query the archive)."
            )

        if warnings:
            warn_msg = " ".join(warnings)
            am["_warning"] = warn_msg
            log.warning("AGENT_MEMORY_LARGE: agent=%s %s", task.owner, warn_msg)

        agent_memory = am

        # Update last_used_at for all injected memories so recency ranking stays current.
        injected_ids = [m.id for m in all_injected]
        session.execute(
            update(AgentMemory)
            .where(AgentMemory.id.in_(injected_ids))
            .values(last_used_at=datetime.now(timezone.utc))
        )

    branch = f"agent/backend/{task_id}"

    # v0.3 adaptive lifecycle: resumption context
    is_resumption = task.checkpoint is not None
    child_outputs: list[dict] = []
    if is_resumption:
        children = (
            session.execute(select(Task).where(Task.parent_task_id == task_id)).scalars().all()
        )
        child_outputs = [
            {
                "task_id": c.id,
                "title": c.title,
                "outputs": c.outputs,
                "status": c.status,
            }
            for c in children
            if c.status in TERMINAL_STATUSES
        ]

    pkg: dict = {
        "schema_version": 1,
        "task_id": task_id,
        "packaged_at": datetime.now(timezone.utc).isoformat(),
        "task": {
            "id": task.id,
            "title": task.title,
            "owner": task.owner,
            "status": task.status,
            "depends_on": task.depends_on,
            "inputs": task.inputs,
            "outputs": task.outputs,
            "acceptance": task.acceptance,
            "risk_tier": task.risk_tier,
            "budget": task.budget,
        },
        "input_artifacts": input_artifacts,
        "adrs": adrs,
        "agent_instructions": {
            "branch": branch,
            "commit_prefix": f"[{task_id}]",
            "read_scope": task.inputs,
            "write_scope": task.outputs,
            "acceptance_criteria": task.acceptance,
        },
        # v0.3 resumption fields (always present so agents can check without KeyError)
        "is_resumption": is_resumption,
        "checkpoint": task.checkpoint,
        "child_outputs": child_outputs,
    }
    if agent_memory is not None:
        pkg["agent_memory"] = agent_memory
    return pkg


def create_run(
    session: Session,
    task_id: str,
    agent_id: str,
    repo_path: Path,
    store_dir: Path,
    retry_count: int = 0,
) -> Run:
    """Build and persist a context package; insert and return the new Run row.

    Writes the package JSON to {store_dir}/{run_id}.json, then adds a Run
    row to the session. The caller must commit the transaction.

    Args:
        session:     An open SQLAlchemy Session.
        task_id:     Task being run.
        agent_id:    Identity of the agent that will consume this run.
        repo_path:   Root of the managed Git repo (for reading artifacts).
        store_dir:   Directory where the context package JSON is written.
        retry_count: Which attempt this is (0 = first, 1 = first retry, …).
                     Retry attempts get a fresh branch with a -retry-{n} suffix.

    Raises:
        TaskNotFoundError: task_id is not in the tasks table.
    """
    from sqlalchemy import func

    run_id = uuid.uuid4()
    package = build_context_package(session, task_id, repo_path)
    package["run_id"] = str(run_id)

    # Derive branch from agent type.
    # Resumed tasks get a -resume-N suffix so each resumption lands on a fresh branch;
    # retried tasks keep the existing -retry-N convention.
    agent_type = agent_id.removesuffix("-agent")
    if package.get("is_resumption"):
        run_count = (
            session.execute(select(func.count(Run.run_id)).where(Run.task_id == task_id)).scalar()
            or 0
        )
        suffix = f"-resume-{run_count}"
    elif retry_count > 0:
        suffix = f"-retry-{retry_count}"
    else:
        suffix = ""
    branch = f"agent/{agent_type}/{task_id}{suffix}"
    package["agent_instructions"]["branch"] = branch
    package["agent_instructions"]["agent_id"] = agent_id

    # Capability token: child tasks get a narrowed scope = child.outputs ∩ parent.outputs;
    # root tasks get a token covering their full outputs list.
    write_scope = package["agent_instructions"]["write_scope"]
    task_orm = session.get(Task, task_id)  # identity-map hit — no extra query
    parent_task_id = task_orm.parent_task_id if task_orm else None
    if parent_task_id:
        parent_task = session.get(Task, parent_task_id)
        parent_write_scope = parent_task.outputs if parent_task else []
        try:
            package["capability_token"] = mint_child_capability_token(
                str(run_id),
                task_id,
                agent_id,
                write_scope,
                parent_write_scope,
                package["task"]["budget"],
            )
        except CapabilityError:
            log.warning(
                "Child task %s outputs outside parent %s scope; falling back to full scope",
                task_id,
                parent_task_id,
            )
            package["capability_token"] = mint_token(
                str(run_id), task_id, agent_id, write_scope, package["task"]["budget"]
            )
    else:
        package["capability_token"] = mint_token(
            str(run_id), task_id, agent_id, write_scope, package["task"]["budget"]
        )

    store_dir.mkdir(parents=True, exist_ok=True)
    package_path = store_dir / f"{run_id}.json"
    package_path.write_text(json.dumps(package, indent=2, default=str), encoding="utf-8")

    branch = package["agent_instructions"]["branch"]
    now = datetime.now(timezone.utc)

    run = Run(
        run_id=run_id,
        schema_version=1,
        task_id=task_id,
        agent_id=agent_id,
        branch=branch,
        context_package_ref=str(package_path),
        started_at=now,
    )
    session.add(run)
    session.flush()
    return run
