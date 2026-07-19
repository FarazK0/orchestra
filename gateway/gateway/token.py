"""Capability token verification (gateway side).

Verifies HS256 JWTs minted by the orchestrator at run creation.
Both sides share CAPABILITY_SECRET from the environment.
"""

from __future__ import annotations

import os

import jwt


def verify_token(token_str: str) -> dict:
    """Decode and verify the JWT, returning the claims dict.

    Raises jwt.InvalidTokenError (or a subclass) if the token is invalid,
    expired, has a wrong algorithm, or if CAPABILITY_SECRET is not set.
    """
    secret = os.getenv("CAPABILITY_SECRET", "")
    if not secret:
        raise jwt.InvalidTokenError("CAPABILITY_SECRET not configured")
    return jwt.decode(token_str, secret, algorithms=["HS256"])
