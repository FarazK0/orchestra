"""Risk-tier policy loader.

Reads `permissions/policy.yaml` (relative to the repo root, resolved from this file's
location) and provides `tier_for_outputs()` to compute the effective risk tier for a task
based on its output paths.

The effective tier is the MAX tier of all matched output paths. This ensures that a task
touching both a docs file (tier 0) and a migration (tier 2) is treated as tier 2 overall.

The policy file is optional. If absent, all tasks fall back to default_tier (1).
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Default location: <repo_root>/permissions/policy.yaml
_DEFAULT_POLICY_PATH = Path(__file__).parent.parent.parent / "permissions" / "policy.yaml"

_policy: "Policy | None" = None


class Policy:
    """Parsed representation of permissions/policy.yaml."""

    def __init__(self, rules: list[dict[str, Any]], default_tier: int = 1) -> None:
        self._rules: list[tuple[str, int]] = [(r["pattern"], int(r["tier"])) for r in rules]
        self.default_tier = default_tier

    def tier_for_path(self, path: str) -> int:
        """Return the tier for a single output path (first-match wins)."""
        for pattern, tier in self._rules:
            if fnmatch(path, pattern):
                return tier
        return self.default_tier

    def tier_for_outputs(self, outputs: list[str]) -> int:
        """Return max tier across all output paths. Returns default_tier when outputs is empty."""
        if not outputs:
            return self.default_tier
        return max(self.tier_for_path(p) for p in outputs)


def load_policy(policy_path: Path | None = None) -> Policy:
    """Load and parse the policy file. Returns a permissive Policy on any error."""
    path = policy_path or _DEFAULT_POLICY_PATH
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        rules = data.get("rules", [])
        default_tier = int(data.get("default_tier", 1))
        log.info("Loaded risk-tier policy from %s (%d rules)", path, len(rules))
        return Policy(rules=rules, default_tier=default_tier)
    except FileNotFoundError:
        log.debug("Policy file not found at %s — using default_tier=1", path)
        return Policy(rules=[], default_tier=1)
    except Exception:
        log.warning("Failed to parse policy file %s — using default_tier=1", path, exc_info=True)
        return Policy(rules=[], default_tier=1)


def get_policy() -> Policy:
    """Return the cached Policy, loading it on first call."""
    global _policy
    if _policy is None:
        _policy = load_policy()
    return _policy


def reload_policy(policy_path: Path | None = None) -> Policy:
    """Force a reload of the policy file (used in tests and CLI reload)."""
    global _policy
    _policy = load_policy(policy_path)
    return _policy
