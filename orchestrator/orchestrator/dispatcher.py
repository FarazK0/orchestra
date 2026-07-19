"""Orchestrator event dispatcher: event-driven task dispatch and DAG scheduling.

Subscribes to ``orchestra:events`` as consumer group ``"orchestrator"`` and reacts to:
- TASK_ASSIGNED   -> create run context, check concurrency guard, launch agent subprocess
- TASK_VALIDATED  -> auto-merge Tier 0 tasks; advance ready DAG successors
- TASK_COMPLETED / TASK_MERGED -> advance ready DAG successors to assigned
- TASK_FAILED     -> retry within budget or escalate

Run with:
    python -m orchestrator.orchestrator.dispatcher

Required env var:
    SANDBOX_REPO_PATH   path to the managed Git repo

Optional env vars (defaults shown):
    REDIS_URL       redis://localhost:6380
    DATABASE_URL    postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra
    RUN_STORE_DIR   /tmp/orchestra/runs
    GATEWAY_URL     http://localhost:8081
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx

from sqlalchemy import select
from sqlalchemy.orm import Session

from .context_packager import create_run
from .dag import get_ready_successors, get_running_conflicts
from .db import AuditRow, Event, Run, Task, get_engine, get_session_factory
from .scheduler import Scheduler
from .state_machine import transition
from .streams import STREAM_KEY, StreamConsumer, StreamPublisher

log = logging.getLogger(__name__)

_RECOVER_INTERVAL = 30  # call _recover_stale every this many consume_one iterations

# Maps agent_id to the Python module used to launch that agent.
# Unknown agent_ids fall back to the backend agent.
_AGENT_MODULES: dict[str, str] = {
    "backend-agent": "agents.backend.main",
    "frontend-agent": "agents.frontend.main",
    "qa-agent": "agents.qa.main",
    "claude-code-agent": "agents.claude_code.main",
}


class Dispatcher:
    def __init__(
        self,
        redis_url: str,
        session_factory,
        repo_path: Path,
        store_dir: Path,
        gateway_url: str = "http://localhost:8081",
    ) -> None:
        self._consumer = StreamConsumer("orchestrator", "dispatcher-0", session_factory, redis_url)
        self._publisher = StreamPublisher(redis_url)
        self._repo_path = repo_path
        self._store_dir = store_dir
        self._session_factory = session_factory
        self._gateway_url = gateway_url
        self._tick = 0
        self._scheduler = Scheduler()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Run the dispatcher loop until interrupted."""
        self._consumer.ensure_group()
        log.info("Dispatcher started on stream %s", STREAM_KEY)
        while True:
            self._consumer.reclaim_pending()
            self._consumer.consume_one(self._handle)
            self._tick += 1
            if self._tick % _RECOVER_INTERVAL == 0:
                self._recover_stale()

    # ------------------------------------------------------------------
    # Stream handler
    # ------------------------------------------------------------------

    def _handle(self, fields: dict) -> None:
        event_type = fields.get("event_type", "")
        task_id = fields.get("task_id", "")
        if not task_id:
            return
        with self._session_factory() as session:
            if event_type == "TASK_ASSIGNED":
                self._on_task_assigned(task_id, session)
            elif event_type in ("TASK_COMPLETED", "TASK_MERGED"):
                self._on_task_completed(task_id, session)
            elif event_type == "TASK_VALIDATED":
                self._on_task_validated(task_id, session)
            elif event_type == "TASK_FAILED":
                self._on_task_failed(task_id, session)
            elif event_type == "TASK_DISCOVERED":
                self._on_task_discovered(task_id, session)

    def _on_task_assigned(self, task_id: str, session: Session) -> None:
        """Create a run and launch the agent subprocess, unless blocked by a conflict."""
        task = session.get(Task, task_id)
        if task is None or task.status != "assigned":
            return  # stale or already dispatched
        conflicts = get_running_conflicts(task, session)
        if conflicts:
            log.info(
                "Task %s blocked by running tasks with overlapping outputs: %s",
                task_id,
                [t.id for t in conflicts],
            )
            return  # _recover_stale will retry once conflicts clear
        run = create_run(session, task_id, task.owner, self._repo_path, self._store_dir)
        run.log_path = str(self._store_dir / "logs" / f"{run.run_id}.log")
        transition(session, task_id, "running", actor="dispatcher")
        session.commit()
        self._launch_agent(run)

    def _on_task_failed(self, task_id: str, session: Session) -> None:
        """Retry the task or escalate it when the retry budget is exhausted."""
        task = session.get(Task, task_id)
        if task is None or task.status != "failed":
            return  # stale event
        budget_retries: int = task.budget.get("retries", 0)
        if task.retry_count < budget_retries:
            task.retry_count += 1
            run = create_run(
                session,
                task_id,
                task.owner,
                self._repo_path,
                self._store_dir,
                retry_count=task.retry_count,
            )
            run.log_path = str(self._store_dir / "logs" / f"{run.run_id}.log")
            event = transition(session, task_id, "running", actor="dispatcher")
            session.commit()
            self._publisher.publish(
                str(event.event_id), "TASK_RETRIED", task_id, {"attempt": task.retry_count}
            )
            self._launch_agent(run)
            log.info("Retrying task %s (attempt %d/%d)", task_id, task.retry_count, budget_retries)
        else:
            event = transition(session, task_id, "escalated", actor="dispatcher")
            session.commit()
            self._publisher.publish(
                str(event.event_id),
                "TASK_ESCALATED",
                task_id,
                {"reason": "retries_exhausted", "retry_count": task.retry_count},
            )
            log.warning("Task %s escalated after %d failed attempts", task_id, task.retry_count)

    def _on_task_validated(self, task_id: str, session: Session) -> None:
        """Auto-merge Tier 0 tasks; always advance DAG successors."""
        task = session.get(Task, task_id)
        if task is not None and task.status == "validated" and task.risk_tier == 0:
            try:
                self._auto_merge(task_id, task, session)
            except Exception:
                log.exception(
                    "Auto-merge failed for task %s; task stays validated for manual merge",
                    task_id,
                )
        self._on_task_completed(task_id, session)

    def _auto_merge(self, task_id: str, task: Task, session: Session) -> None:
        """Merge a Tier 0 validated task via the gateway without human approval."""
        run = (
            session.execute(
                select(Run).where(Run.task_id == task_id).order_by(Run.started_at.desc())
            )
            .scalars()
            .first()
        )
        branch = (
            run.branch
            if run is not None
            else f"agent/{task.owner.removesuffix('-agent')}/{task_id}"
        )
        with httpx.Client(timeout=60.0) as http:
            http.post(
                f"{self._gateway_url}/git/merge",
                json={
                    "actor": "dispatcher",
                    "task_id": task_id,
                    "repo_path": str(self._repo_path),
                    "branch": branch,
                },
            ).raise_for_status()
        merged_ev = transition(session, task_id, "merged", actor="dispatcher")
        closed_ev = transition(session, task_id, "closed", actor="dispatcher")
        session.commit()
        self._publisher.publish(
            str(merged_ev.event_id), "TASK_MERGED", task_id, {"auto_merged": True}
        )
        self._publisher.publish(
            str(closed_ev.event_id), "TASK_CLOSED", task_id, {"auto_merged": True}
        )
        log.info("Auto-merged Tier 0 task %s (branch %s)", task_id, branch)

    def _write_episode_memory(self, task_id: str, session: Session) -> None:
        """Summarise the completed task's audit trail and persist as episode memory."""
        task = session.get(Task, task_id)
        if task is None:
            return
        audit_rows = session.query(AuditRow).filter(AuditRow.task_id == task_id).all()

        # Build a structured (template-based) summary — no LLM required.
        files_written = [
            r.details.get("path", "?") for r in audit_rows if r.action == "gateway:write_artifact"
        ]
        files_read = [
            r.details.get("path", "?") for r in audit_rows if r.action == "gateway:read_artifact"
        ]
        commands_run = [
            " ".join(r.details.get("command", []))
            for r in audit_rows
            if r.action == "gateway:run_command"
        ]
        commit_sha = next(
            (r.details.get("sha", "") for r in audit_rows if r.action == "gateway:git_commit"),
            "",
        )

        lines = [f"## {task_id}: {task.title}", f"Agent: {task.owner}"]
        if files_written:
            lines.append(f"Files written: {', '.join(files_written)}")
        if files_read:
            lines.append(f"Files read: {', '.join(files_read)}")
        if commands_run:
            lines.append(f"Commands run: {'; '.join(commands_run)}")
        if commit_sha:
            lines.append(f"Commit: {commit_sha}")

        content = "\n".join(lines)[:2000]
        try:
            resp = httpx.post(
                f"{self._gateway_url}/memory/upsert",
                json={
                    "task_id": task_id,
                    "agent_id": task.owner,
                    "project_id": "default",
                    "memory_type": "episode",
                    "key": f"episode/{task_id}",
                    "content": content,
                },
                headers={"X-Platform-Actor": "dispatcher"},
                timeout=10.0,
            )
            resp.raise_for_status()
            log.info("Episode memory written for task %s (agent %s)", task_id, task.owner)
        except Exception as exc:
            log.warning("Failed to write episode memory for %s: %s", task_id, exc)

    def _on_task_discovered(self, task_id: str, session: Session) -> None:
        """Process a TASK_DISCOVERED event: create child task, block parent."""
        discovery_event = session.execute(
            select(Event)
            .where(Event.task_id == task_id, Event.event_type == "TASK_DISCOVERED")
            .order_by(Event.emitted_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if discovery_event is None:
            log.warning("No TASK_DISCOVERED event found for task %s", task_id)
            return

        child = self._scheduler.handle_task_discovered(session, discovery_event)
        if child is None:
            session.commit()  # persist any rejection events
            return

        assign_event = transition(session, child.id, "assigned", actor="dispatcher")
        session.commit()

        self._publisher.publish(
            str(uuid.uuid4()),
            "TASK_CREATED",
            child.id,
            {"parent_task_id": task_id, "title": child.title},
        )
        self._publisher.publish(str(assign_event.event_id), "TASK_ASSIGNED", child.id, {})
        log.info(
            "Task discovered: child %s created and assigned; parent %s blocked",
            child.id,
            task_id,
        )

    def _on_task_completed(self, task_id: str, session: Session) -> None:
        """Write episode memory, advance DAG successors, and unblock waiting parents."""
        self._write_episode_memory(task_id, session)
        successors = get_ready_successors(task_id, session)
        for succ in successors:
            event = transition(session, succ.id, "assigned", actor="dispatcher")
            session.commit()
            self._publisher.publish(str(event.event_id), "TASK_ASSIGNED", succ.id, {})
            log.info("Auto-assigned successor %s (unblocked by %s)", succ.id, task_id)

        resumed = self._scheduler.on_child_terminal(session, task_id)
        if resumed:
            session.commit()
            for parent in resumed:
                self._publisher.publish(
                    str(uuid.uuid4()), "TASK_RESUMED", parent.id, {"unblocked_by": task_id}
                )
                self._publisher.publish(str(uuid.uuid4()), "TASK_ASSIGNED", parent.id, {})
                log.info("Parent %s resumed after child %s", parent.id, task_id)

    # ------------------------------------------------------------------
    # Agent launch
    # ------------------------------------------------------------------

    def _launch_agent(self, run: Run) -> None:
        agent_type = os.getenv("AGENT_TYPE", "claude-code")
        if (
            agent_type != "python"
            and run.agent_id in _AGENT_MODULES
            and run.agent_id != "claude-code-agent"
        ):
            module = "agents.claude_code.main"
        else:
            module = _AGENT_MODULES.get(run.agent_id, "agents.backend.main")
        log_path = run.log_path or str(self._store_dir / "logs" / f"{run.run_id}.log")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "w")  # noqa: SIM115
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                module,
                "--context",
                run.context_package_ref,
                "--run-id",
                str(run.run_id),
                "--repo",
                str(self._repo_path),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        log.info(
            "Launched %s for run %s (task %s) log=%s", module, run.run_id, run.task_id, log_path
        )

    # ------------------------------------------------------------------
    # Stale-task recovery
    # ------------------------------------------------------------------

    def _recover_stale(self) -> None:
        """Re-publish TASK_ASSIGNED for assigned tasks with no active run.

        Handles the rare race where the publish from api.py reached the stream
        before the DB commit, causing the handler to see the task in the wrong
        state and XACK the message without dispatching.
        """
        with self._session_factory() as session:
            assigned = (
                session.execute(select(Task).where(Task.status == "assigned")).scalars().all()
            )
            active_task_ids = {
                r.task_id
                for r in session.execute(select(Run).where(Run.finished_at.is_(None)))
                .scalars()
                .all()
            }
            for task in assigned:
                if task.id not in active_task_ids:
                    conflicts = get_running_conflicts(task, session)
                    if not conflicts:
                        self._publisher.publish(str(uuid.uuid4()), "TASK_ASSIGNED", task.id, {})
                        log.info("Recovered stale assigned task %s", task.id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6380")
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra",
    )
    repo_path_str = os.getenv("SANDBOX_REPO_PATH")
    if not repo_path_str:
        log.error("SANDBOX_REPO_PATH env var is required")
        sys.exit(1)
    store_dir_str = os.getenv("RUN_STORE_DIR", "/tmp/orchestra/runs")
    repo_path = Path(repo_path_str)
    store_dir = Path(store_dir_str)
    store_dir.mkdir(parents=True, exist_ok=True)

    gateway_url = os.getenv("GATEWAY_URL", "http://localhost:8081")
    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    Dispatcher(redis_url, factory, repo_path, store_dir, gateway_url=gateway_url).start()


if __name__ == "__main__":
    main()
