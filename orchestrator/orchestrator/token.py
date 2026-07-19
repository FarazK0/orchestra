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
