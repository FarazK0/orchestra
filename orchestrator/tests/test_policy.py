"""Tests for the risk-tier policy loader."""

from __future__ import annotations

from pathlib import Path

import yaml

from orchestrator.orchestrator.policy import Policy, load_policy, reload_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_policy(tmp_path: Path, rules: list, default_tier: int = 1) -> Path:
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.dump({"version": 1, "rules": rules, "default_tier": default_tier}))
    return p


# ---------------------------------------------------------------------------
# Policy.tier_for_outputs
# ---------------------------------------------------------------------------


def test_tier_for_outputs_returns_max(tmp_path):
    """A task touching both a tier-0 and a tier-2 path gets tier 2."""
    pol = load_policy(
        _write_policy(
            tmp_path,
            [
                {"pattern": "docs/adr/**", "tier": 0},
                {"pattern": "schemas/**", "tier": 2},
            ],
        )
    )
    assert pol.tier_for_outputs(["docs/adr/ADR-001.md", "schemas/task.json"]) == 2


def test_tier_for_outputs_empty_returns_default(tmp_path):
    pol = load_policy(_write_policy(tmp_path, [], default_tier=1))
    assert pol.tier_for_outputs([]) == 1


def test_tier_for_outputs_no_match_returns_default(tmp_path):
    pol = load_policy(_write_policy(tmp_path, [{"pattern": "docs/**", "tier": 0}], default_tier=1))
    assert pol.tier_for_outputs(["src/app.py"]) == 1


def test_policy_self_referential(tmp_path):
    """permissions/** is tier 2 — the policy file itself is protected."""
    pol = load_policy(_write_policy(tmp_path, [{"pattern": "permissions/**", "tier": 2}]))
    assert pol.tier_for_outputs(["permissions/policy.yaml"]) == 2


def test_tier_for_path_first_match_wins(tmp_path):
    """When multiple rules match, the first rule in the list wins."""
    pol = load_policy(
        _write_policy(
            tmp_path,
            [
                {"pattern": "docs/adr/**", "tier": 0},
                {"pattern": "docs/**", "tier": 1},
            ],
        )
    )
    assert pol.tier_for_path("docs/adr/ADR-001.md") == 0
    assert pol.tier_for_path("docs/design/spec.md") == 1


def test_explicit_tier_not_lowered_by_policy():
    """Policy tier_for_outputs returns the policy value; caller uses max()."""
    pol = Policy(rules=[{"pattern": "docs/**", "tier": 0}], default_tier=1)
    # Simulate what api.py does: max(explicit_tier=2, policy_tier)
    explicit = 2
    policy_tier = pol.tier_for_outputs(["docs/foo.md"])
    assert max(explicit, policy_tier) == 2


# ---------------------------------------------------------------------------
# load_policy — file-not-found and parse errors
# ---------------------------------------------------------------------------


def test_load_policy_missing_file():
    """Absent policy file → default Policy(default_tier=1), no exception."""
    pol = load_policy(Path("/nonexistent/path/policy.yaml"))
    assert pol.default_tier == 1
    assert pol.tier_for_outputs(["anything.py"]) == 1


def test_load_policy_malformed_yaml(tmp_path):
    """Malformed YAML → fallback Policy, no exception."""
    bad = tmp_path / "policy.yaml"
    bad.write_text(": : : invalid yaml :\n  - [")
    pol = load_policy(bad)
    assert pol.default_tier == 1


# ---------------------------------------------------------------------------
# reload_policy (test isolation helper)
# ---------------------------------------------------------------------------


def test_reload_policy_updates_singleton(tmp_path):
    p1 = _write_policy(tmp_path, [{"pattern": "docs/**", "tier": 0}])
    pol1 = reload_policy(p1)
    assert pol1.tier_for_outputs(["docs/foo.md"]) == 0

    d2 = tmp_path / "v2"
    d2.mkdir()
    p2 = _write_policy(d2, [{"pattern": "docs/**", "tier": 1}])
    pol2 = reload_policy(p2)
    assert pol2.tier_for_outputs(["docs/foo.md"]) == 1
