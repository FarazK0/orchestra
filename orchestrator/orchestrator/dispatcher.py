"""Orchestrator event dispatcher: event-driven task dispatch and DAG scheduling.

Subscribes to ``orchestra:events`` as consumer group ``"orchestrator"`` and reacts to:
- TASK_ASSIGNED  -> create run context, check concurrency guard, launch agent subprocess
- TASK_COMPLETED / TASK_VALIDATED / TASK_MERGED -> advance ready DAG successors to assigned

Run with:
    python -m orchestrator.orchestrator.dispatcher

Required env var:
    SANDBOX_REPO_PATH   path to the managed Git repo

Optional env vars (defaults shown):
    REDIS_URL       redis://localhost:6380
    DATABASE_URL    postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra
    RUN_STORE_DIR   /tmp/orchestra/runs
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .context_packager import create_run
from .dag import get_ready_successors, get_running_conflicts
from .db import Run, Task, get_engine, get_session_factory
from .state_machine import transition
from .streams import STREAM_KEY, StreamConsumer, StreamPublisher

log = logging.getLogger(__name__)

_RECOVER_INTERVAL = 30  # call _recover_stale every this many consume_one iterations

# Maps agent_id to the Python module used to launch that agent.
# Unknown agent_ids fall back to the backend agent.
_AGENT_MODULES: dict[str, str] = {
    "backend-agent": "agents.backend.main",
    "frontend-agent": "agents.frontend.main",
}


class Dispatcher:
    def __init__(
        self,
        redis_url: str,
        session_factory,
        repo_path: Path,
        store_dir: Path,
    ) -> None:
        self._consumer = StreamConsumer("orchestrator", "dispatcher-0", session_factory, redis_url)
        self._publisher = StreamPublisher(redis_url)
        self._repo_path = repo_path
        self._store_dir = store_dir
        self._session_factory = session_factory
        self._tick = 0

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
            elif event_type in ("TASK_COMPLETED", "TASK_VALIDATED", "TASK_MERGED"):
                self._on_task_completed(task_id, session)

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
        transition(session, task_id, "running", actor="dispatcher")
        session.commit()
        self._launch_agent(run)

    def _on_task_completed(self, task_id: str, session: Session) -> None:
        """Advance any DAG successors that are now fully unblocked."""
        successors = get_ready_successors(task_id, session)
        for succ in successors:
            event = transition(session, succ.id, "assigned", actor="dispatcher")
            session.commit()
            self._publisher.publish(str(event.event_id), "TASK_ASSIGNED", succ.id, {})
            log.info("Auto-assigned successor %s (unblocked by %s)", succ.id, task_id)

    # ------------------------------------------------------------------
    # Agent launch
    # ------------------------------------------------------------------

    def _launch_agent(self, run: Run) -> None:
        module = _AGENT_MODULES.get(run.agent_id, "agents.backend.main")
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
            ]
        )
        log.info("Launched %s for run %s (task %s)", module, run.run_id, run.task_id)

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

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    Dispatcher(redis_url, factory, repo_path, store_dir).start()


if __name__ == "__main__":
    main()
