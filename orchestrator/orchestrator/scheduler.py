"""Scheduler: processes TASK_DISCOVERED events and manages parent/child task lifecycle.

Responsibilities
----------------
- Validate incoming TASK_DISCOVERED events (depth, duplicate, circular dep).
- Create child tasks and suspend (block) their parent.
- Resume parents when all their blocking children reach a terminal state.

Callers (Dispatcher) own the DB session commit and Redis publishing; the
Scheduler only mutates DB state and returns what changed.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import AuditRow, Event, Task
from .state_machine import transition

log = logging.getLogger(__name__)

MAX_SPAWN_DEPTH: int = int(os.getenv("ORCHESTRA_MAX_SPAWN_DEPTH", "5"))


def _next_task_id(session: Session) -> str:
    from sqlalchemy import text

    n = session.execute(
        text(
            "SELECT COALESCE(MAX(CAST(SUBSTR(id, 6) AS INTEGER)), 0)"
            " FROM tasks WHERE id ~ '^TASK-[0-9]+$'"
        )
    ).scalar()
    return f"TASK-{(n or 0) + 1:03d}"


class Scheduler:
    """In-process scheduler; instantiated once inside the Dispatcher."""

    def handle_task_discovered(self, session: Session, event: Event) -> Task | None:
        """Validate a TASK_DISCOVERED event and mutate the DAG.

        Expected payload fields:
          parent_task_id  str       -- the task that emitted the discovery
          title           str       -- short title for the new child task
          owner_hint      str       -- which agent type should run the child
          reason          str       -- why this work is needed
          outputs         list[str] -- files the child will write
          dependencies    list[str] -- task IDs the child depends on (optional)
          checkpoint      dict      -- agent state to restore on resume (optional)

        Returns the newly created (uncommitted) child Task, or None if the
        discovery was rejected. The caller MUST commit and publish.
        """
        payload = event.payload
        parent_task_id: str = payload.get("parent_task_id") or event.task_id or ""
        title: str = payload.get("title", "")
        owner_hint: str = payload.get("owner_hint", "backend-agent")
        reason: str = payload.get("reason", "")
        dependencies: list[str] = payload.get("dependencies") or []
        outputs: list[str] = payload.get("outputs") or []
        checkpoint: dict | None = payload.get("checkpoint")

        if not parent_task_id:
            log.warning("TASK_DISCOVERED: no parent_task_id in payload; ignoring")
            return None

        # 1. Load and validate parent
        parent = session.get(Task, parent_task_id)
        if parent is None:
            log.warning("TASK_DISCOVERED: parent task %s not found", parent_task_id)
            return None
        if parent.status != "running":
            log.warning(
                "TASK_DISCOVERED: parent %s status=%s (expected running)",
                parent_task_id,
                parent.status,
            )
            return None

        # 2. Depth guard
        if parent.spawn_depth >= MAX_SPAWN_DEPTH:
            log.warning(
                "TASK_DISCOVERED: depth %d >= MAX_SPAWN_DEPTH %d; rejecting",
                parent.spawn_depth,
                MAX_SPAWN_DEPTH,
            )
            self._emit_rejection(session, parent_task_id, "max_spawn_depth_exceeded", reason)
            return None

        # 3. Duplicate guard: non-cancelled child with same title under this parent
        existing = session.execute(
            select(Task).where(
                Task.parent_task_id == parent_task_id,
                Task.title == title,
                Task.status != "cancelled",
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.warning(
                "TASK_DISCOVERED: duplicate title %r under parent %s; rejecting",
                title,
                parent_task_id,
            )
            self._emit_rejection(session, parent_task_id, "duplicate_task", reason)
            return None

        # 4. Validate dependency IDs exist
        for dep_id in dependencies:
            dep = session.get(Task, dep_id)
            if dep is None:
                log.warning("TASK_DISCOVERED: unknown dependency %s; rejecting", dep_id)
                self._emit_rejection(session, parent_task_id, "unknown_dependency", reason)
                return None

        # 5. Circular dependency check: child must not depend on its ancestors
        ancestor_ids = self._get_ancestor_ids(session, parent_task_id)
        ancestor_ids.add(parent_task_id)
        for dep_id in dependencies:
            if dep_id in ancestor_ids:
                log.warning("TASK_DISCOVERED: circular dependency through %s; rejecting", dep_id)
                self._emit_rejection(session, parent_task_id, "circular_dependency", reason)
                return None

        # 5b. Capability inheritance gate: child outputs must be within parent write_scope
        if outputs and parent.outputs:
            from .token import _intersect_scopes

            allowed = _intersect_scopes(outputs, parent.outputs)
            if len(allowed) < len(outputs):
                out_of_scope = [o for o in outputs if o not in allowed]
                log.warning(
                    "TASK_DISCOVERED: child outputs %s outside parent scope %s; rejecting",
                    out_of_scope,
                    parent.outputs,
                )
                self._emit_rejection(
                    session, parent_task_id, "outputs_outside_parent_scope", reason
                )
                return None

        # 6. Create child task
        child_id = _next_task_id(session)
        now = datetime.now(timezone.utc)
        child = Task(
            id=child_id,
            schema_version=1,
            title=title,
            owner=owner_hint,
            status="created",
            depends_on=dependencies,
            inputs=[],
            outputs=outputs,
            acceptance=[f"Complete: {reason}"] if reason else [],
            risk_tier=parent.risk_tier,
            budget=parent.budget,
            parent_task_id=parent_task_id,
            spawn_depth=parent.spawn_depth + 1,
            blocked_by=[],
            checkpoint=None,
            created_at=now,
            updated_at=now,
        )
        session.add(child)
        session.flush()  # assigns child.id in identity map

        # 7. Transition parent: running → blocked
        transition(
            session,
            parent_task_id,
            "blocked",
            actor="scheduler",
            payload={"child_task_id": child_id, "reason": reason},
        )

        # 8. Store blocked_by and checkpoint on parent
        parent.blocked_by = [child_id]
        parent.checkpoint = checkpoint
        session.flush()

        log.info(
            "Task discovered: created child %s (depth=%d), blocked parent %s",
            child_id,
            child.spawn_depth,
            parent_task_id,
        )
        return child

    def on_child_terminal(self, session: Session, child_id: str) -> list[Task]:
        """Remove *child_id* from its parent's blocked_by list.

        If blocked_by becomes empty the parent is transitioned blocked → assigned
        and returned. The caller MUST commit and publish TASK_ASSIGNED after this.

        Returns the list of tasks that were unblocked (0 or 1 entries today; the
        data model supports multiple blockers for future use).
        """
        child = session.get(Task, child_id)
        if child is None or not child.parent_task_id:
            return []

        parent = session.get(Task, child.parent_task_id)
        if parent is None or parent.status != "blocked":
            return []

        current_blocked_by: list[str] = list(parent.blocked_by or [])
        if child_id not in current_blocked_by:
            return []

        new_blocked_by = [bid for bid in current_blocked_by if bid != child_id]
        parent.blocked_by = new_blocked_by
        session.flush()

        if new_blocked_by:
            log.info(
                "Child %s terminal; parent %s still blocked by %s",
                child_id,
                parent.id,
                new_blocked_by,
            )
            return []

        # All children done — resume parent
        transition(session, parent.id, "assigned", actor="scheduler")
        log.info("Parent %s unblocked by child %s; transitioned to assigned", parent.id, child_id)
        return [parent]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_rejection(
        self, session: Session, parent_task_id: str, reason: str, detail: str
    ) -> None:
        now = datetime.now(timezone.utc)
        ev = Event(
            event_id=uuid.uuid4(),
            schema_version=1,
            event_type="TASK_DISCOVERY_REJECTED",
            task_id=parent_task_id,
            emitted_by="scheduler",
            emitted_at=now,
            payload={"reason": reason, "detail": detail},
        )
        session.add(ev)
        session.flush()
        audit = AuditRow(
            id=uuid.uuid4(),
            timestamp=now,
            actor="scheduler",
            action="task_discovery_rejected",
            task_id=parent_task_id,
            event_id=ev.event_id,
            details={"reason": reason},
        )
        session.add(audit)
        session.flush()

    def _get_ancestor_ids(self, session: Session, task_id: str) -> set[str]:
        """Return all ancestor task IDs by following the parent_task_id chain."""
        ancestors: set[str] = set()
        current_id = task_id
        while True:
            task = session.get(Task, current_id)
            if task is None or task.parent_task_id is None:
                break
            if task.parent_task_id in ancestors:
                break  # cycle guard
            ancestors.add(task.parent_task_id)
            current_id = task.parent_task_id
        return ancestors
