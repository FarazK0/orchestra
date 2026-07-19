"""Capability token minting (orchestrator side).

Mints HS256 JWTs scoped to a specific run. Agents present these tokens on
every gateway call; the gateway verifies them before the DB check.

Requires CAPABILITY_SECRET in the environment. If the secret is absent the
function returns an empty string and the gateway falls back to DB-only auth
(backwards-compatible for environments not yet running Phase 3 key management).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import jwt


class CapabilityError(Exception):
    """Raised when a child task requests write scope beyond its parent's."""


def _intersect_scopes(child_outputs: list[str], parent_write_scope: list[str]) -> list[str]:
    """Return elements of child_outputs that fall within parent_write_scope.

    A child output is "within" a parent scope entry when it is equal to the
    entry or is a descendant path (the parent entry is a prefix directory).
    """
    result = []
    for c in child_outputs:
        c_norm = c.rstrip("/")
        for p in parent_write_scope:
            p_norm = p.rstrip("/")
            if c_norm == p_norm or c_norm.startswith(p_norm + "/"):
                result.append(c)
                break
    return result


def mint_token(
    run_id: str,
    task_id: str,
    agent_id: str,
    write_scope: list[str],
    budget: dict[str, Any],
) -> str:
    """Return a signed HS256 JWT embedding run authorisation claims.

    Expiry = now + wall_clock_min + 30-minute grace, capped at 24 hours.
    Returns "" if CAPABILITY_SECRET is not set.
    """
    secret = os.getenv("CAPABILITY_SECRET", "")
    if not secret:
        return ""

    wall_clock_min: int = int(budget.get("wall_clock_min", 30))
    ttl_sec = min((wall_clock_min + 30) * 60, 86_400)
    now = datetime.now(timezone.utc)
    iat = int(now.timestamp())

    payload: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "write_scope": write_scope,
        "iat": iat,
        "exp": iat + ttl_sec,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def mint_child_capability_token(
    run_id: str,
    task_id: str,
    agent_id: str,
    child_outputs: list[str],
    parent_write_scope: list[str],
    budget: dict[str, Any],
) -> str:
    """Mint a token whose write_scope is child_outputs ∩ parent_write_scope.

    Raises CapabilityError if the intersection is empty (no path in
    child_outputs falls within any parent_write_scope entry).
    Returns "" if CAPABILITY_SECRET is not set (same as mint_token).
    """
    secret = os.getenv("CAPABILITY_SECRET", "")
    if not secret:
        return ""

    narrowed = _intersect_scopes(child_outputs, parent_write_scope)
    if not narrowed:
        raise CapabilityError(
            f"Child task {task_id} outputs {child_outputs} have no overlap "
            f"with parent write_scope {parent_write_scope}"
        )
    return mint_token(run_id, task_id, agent_id, narrowed, budget)
