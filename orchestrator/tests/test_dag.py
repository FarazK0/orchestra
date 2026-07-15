"""Tests for the DAG readiness and conflict-detection functions."""

from __future__ import annotations

import pytest

from orchestrator.orchestrator.dag import (
    TERMINAL_STATUSES,
    get_ready_successors,
    get_running_conflicts,
    outputs_overlap,
    task_is_ready,
)

from .conftest import make_task


# ---------------------------------------------------------------------------
# task_is_ready
# ---------------------------------------------------------------------------


def test_task_is_ready_no_deps(session):
    task = make_task(session, "TASK-D01", status="created")
    assert task.depends_on == []
    assert task_is_ready(task, session) is True


@pytest.mark.parametrize("dep_status", sorted(TERMINAL_STATUSES))
def test_task_is_ready_all_terminal(session, dep_status):
    dep = make_task(session, "TASK-D10", status=dep_status)
    task = make_task(session, "TASK-D11", status="created")
    task.depends_on = [dep.id]
    session.flush()
    assert task_is_ready(task, session) is True


def test_task_not_ready_dep_running(session):
    dep = make_task(session, "TASK-D20", status="running")
    task = make_task(session, "TASK-D21", status="created")
    task.depends_on = [dep.id]
    session.flush()
    assert task_is_ready(task, session) is False


def test_task_not_ready_if_one_dep_still_pending(session):
    done = make_task(session, "TASK-D30", status="completed")
    pending = make_task(session, "TASK-D31", status="running")
    task = make_task(session, "TASK-D32", status="created")
    task.depends_on = [done.id, pending.id]
    session.flush()
    assert task_is_ready(task, session) is False


# ---------------------------------------------------------------------------
# get_ready_successors
# ---------------------------------------------------------------------------


def test_get_ready_successors_returns_unblocked(session):
    a = make_task(session, "TASK-D40", status="completed")
    b = make_task(session, "TASK-D41", status="created")
    b.depends_on = [a.id]
    session.flush()
    successors = get_ready_successors(a.id, session)
    assert any(s.id == b.id for s in successors)


def test_get_ready_successors_excludes_still_blocked(session):
    a = make_task(session, "TASK-D50", status="completed")
    d = make_task(session, "TASK-D51", status="running")  # still running
    c = make_task(session, "TASK-D52", status="created")
    c.depends_on = [a.id, d.id]
    session.flush()
    successors = get_ready_successors(a.id, session)
    assert not any(s.id == c.id for s in successors)


def test_get_ready_successors_excludes_non_created(session):
    a = make_task(session, "TASK-D60", status="completed")
    b = make_task(session, "TASK-D61", status="assigned")
    b.depends_on = [a.id]
    session.flush()
    # b is already 'assigned', not 'created' — should not appear
    successors = get_ready_successors(a.id, session)
    assert not any(s.id == b.id for s in successors)


# ---------------------------------------------------------------------------
# outputs_overlap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, expected",
    [
        (["src/auth/"], ["src/auth/"], True),
        (["src/auth/"], ["src/auth/login.py"], True),
        (["src/auth/login.py"], ["src/auth/"], True),
        (["src/auth/"], ["src/frontend/"], False),
        (["src/auth.py"], ["src/auth/util.py"], False),
        ([], ["src/auth/"], False),
        (["src/auth/"], [], False),
    ],
)
def test_outputs_overlap(a, b, expected):
    assert outputs_overlap(a, b) is expected


# ---------------------------------------------------------------------------
# get_running_conflicts
# ---------------------------------------------------------------------------


def test_get_running_conflicts_detects_overlap(session):
    running = make_task(session, "TASK-D70", status="running")
    running.outputs = ["src/auth/"]
    new_task = make_task(session, "TASK-D71", status="assigned")
    new_task.outputs = ["src/auth/login.py"]
    session.flush()
    conflicts = get_running_conflicts(new_task, session)
    assert any(t.id == running.id for t in conflicts)


def test_get_running_conflicts_no_overlap(session):
    running = make_task(session, "TASK-D80", status="running")
    running.outputs = ["src/frontend/"]
    new_task = make_task(session, "TASK-D81", status="assigned")
    new_task.outputs = ["src/auth/"]
    session.flush()
    assert get_running_conflicts(new_task, session) == []


def test_get_running_conflicts_excludes_self(session):
    task = make_task(session, "TASK-D90", status="running")
    task.outputs = ["src/auth/"]
    session.flush()
    assert get_running_conflicts(task, session) == []


def test_get_running_conflicts_no_outputs(session):
    make_task(session, "TASK-D91", status="running")
    task = make_task(session, "TASK-D92", status="assigned")
    # task.outputs == [] by default
    session.flush()
    assert get_running_conflicts(task, session) == []
