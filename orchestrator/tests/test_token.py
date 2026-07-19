"""Tests for orchestrator.token — capability token minting and scope narrowing."""

from __future__ import annotations

import os
from unittest.mock import patch

import jwt
import pytest

from orchestrator.orchestrator.token import (
    CapabilityError,
    _intersect_scopes,
    mint_child_capability_token,
    mint_token,
)

_SECRET = "test-capability-secret-stage4"
_BUDGET = {"tokens": 100_000, "wall_clock_min": 30, "retries": 2}
_ENV = {"CAPABILITY_SECRET": _SECRET}


# ---------------------------------------------------------------------------
# _intersect_scopes unit tests
# ---------------------------------------------------------------------------


def test_intersect_exact_match():
    result = _intersect_scopes(["app/auth.py"], ["app/auth.py"])
    assert result == ["app/auth.py"]


def test_intersect_child_under_parent_dir():
    result = _intersect_scopes(["app/auth/models.py"], ["app/"])
    assert result == ["app/auth/models.py"]


def test_intersect_child_under_parent_dir_no_trailing_slash():
    result = _intersect_scopes(["app/auth/models.py"], ["app"])
    assert result == ["app/auth/models.py"]


def test_intersect_child_outside_parent():
    result = _intersect_scopes(["other/file.py"], ["app/"])
    assert result == []


def test_intersect_partial_overlap():
    result = _intersect_scopes(["app/auth.py", "other/file.py"], ["app/"])
    assert result == ["app/auth.py"]
    assert "other/file.py" not in result


def test_intersect_empty_parent():
    result = _intersect_scopes(["app/auth.py"], [])
    assert result == []


def test_intersect_empty_child():
    result = _intersect_scopes([], ["app/"])
    assert result == []


# ---------------------------------------------------------------------------
# mint_child_capability_token
# ---------------------------------------------------------------------------


def test_mint_child_within_scope():
    with patch.dict(os.environ, _ENV):
        token = mint_child_capability_token(
            "run-1", "TASK-001", "backend-agent", ["app/auth.py"], ["app/"], _BUDGET
        )
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"])
    assert claims["write_scope"] == ["app/auth.py"]
    assert claims["task_id"] == "TASK-001"


def test_mint_child_narrowed_to_overlap_only():
    with patch.dict(os.environ, _ENV):
        token = mint_child_capability_token(
            "run-2",
            "TASK-002",
            "backend-agent",
            ["app/auth.py", "outside/secret.py"],
            ["app/"],
            _BUDGET,
        )
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"])
    assert claims["write_scope"] == ["app/auth.py"]
    assert "outside/secret.py" not in claims["write_scope"]


def test_mint_child_raises_capability_error_when_no_overlap():
    with patch.dict(os.environ, _ENV):
        with pytest.raises(CapabilityError, match="no overlap"):
            mint_child_capability_token(
                "run-3", "TASK-003", "backend-agent", ["outside/file.py"], ["app/"], _BUDGET
            )


def test_mint_child_no_secret_returns_empty():
    with patch.dict(os.environ, {"CAPABILITY_SECRET": ""}):
        token = mint_child_capability_token(
            "run-4", "TASK-004", "backend-agent", ["app/auth.py"], ["app/"], _BUDGET
        )
    assert token == ""


def test_mint_child_depth_1_of_planner_task():
    """Depth-1 child of a depth-0 planner task gets write_scope ⊆ parent scope."""
    parent_scope = ["app/", "tests/"]
    child_outputs = ["app/models.py", "tests/test_models.py"]
    with patch.dict(os.environ, _ENV):
        token = mint_child_capability_token(
            "run-5", "TASK-005", "backend-agent", child_outputs, parent_scope, _BUDGET
        )
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"])
    assert set(claims["write_scope"]) == {"app/models.py", "tests/test_models.py"}


def test_mint_token_root_task_unaffected():
    """Root tasks (no parent) still use mint_token with full write_scope."""
    with patch.dict(os.environ, _ENV):
        token = mint_token("run-6", "TASK-006", "backend-agent", ["app/", "tests/"], _BUDGET)
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"])
    assert set(claims["write_scope"]) == {"app/", "tests/"}
