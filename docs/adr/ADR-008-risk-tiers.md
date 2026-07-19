# ADR-008: Risk-tier policy file and Tier 2 hard gate

**Status:** Accepted  
**Date:** 2026-07-19

## Context

Phase 3 governance requires a policy file to classify tasks by risk level and a hard gate
that blocks high-risk merges until the human explicitly acknowledges the risk. Three tiers
were defined in Phase 1 design:

- **Tier 0** (auto-merge): docs, reports, test additions — merged automatically after
  validation, human notified. (Implemented in Phase 2 dispatcher.)
- **Tier 1** (batch review): ordinary code changes — queued for human review.
- **Tier 2** (blocking approval): architecture changes, schema migrations, permission
  changes, main config edits — hard gate, requires explicit human override.

Before this ADR, risk_tier was stored in the DB but Tier 2 had zero enforcement: any
human could approve a migration the same way as a docs edit.

## Decision

**Policy file: `permissions/policy.yaml` in the orchestra root repo.**

A YAML file with glob-based rules maps output paths to risk tiers. The file lives in the
orchestra repo (not the sandbox), committed to git, and is itself protected by its own
rules (any change to `permissions/**` is Tier 2 by definition).

Rule evaluation: first-match-wins per path; task tier = max across all output paths.
When no rule matches, `default_tier` applies (1 = batch review).

**Tier auto-assignment at task creation.**

`POST /tasks` computes the policy tier from `outputs`; if the caller explicitly passes
`risk_tier`, that value acts as a floor (`max(explicit, policy_tier)`). Passing no
`risk_tier` uses the policy tier directly. This allows a caller to raise the tier above
policy (e.g., treat a docs task as Tier 2 during sensitive periods) but not lower it
(e.g., cannot mark a migration as Tier 0).

**Tier 2 hard gate in `state_machine.py`.**

The `validated → merged` transition rejects with `InvalidTransitionError` if
`task.risk_tier == 2` and `details["tier2_override"]` is not `True`. This:
- Requires no new DB states or schema migration.
- Is auditable: `tier2_override=True` is written into the audit row's `details` field.
- Is enforced at the state machine level (not just the CLI), so all callers are blocked.

**CLI tier-aware UX.**

`orchctl approve` and `orchctl merge` both require `--tier-2-override` for Tier 2 tasks.
The `review` interactive loop requires the human to type the task ID to confirm instead
of a simple y/n. `orchctl list` shows a `[T2]` badge on Tier 2 tasks.

## Consequences

- **Tier 1 behaviour unchanged.** Tier 1 tasks still wait for a human to call
  `orchctl approve` or merge via the review loop. No batch-queue UI yet (Step 29).
- **Policy file is self-referential.** A change to `permissions/policy.yaml` is itself
  a Tier 2 task because the file matches the `permissions/**` rule. This is intentional.
- **Tier 0 behaviour unchanged.** The dispatcher's auto-merge path for Tier 0 is untouched.
- **`risk_tier` is now `Optional[int]` in the `TaskCreate` request body.** `None` means
  "let the policy decide." Explicit integers act as a floor. All existing callers that
  omit `risk_tier` will now receive the policy-computed tier instead of the hardcoded 1.

## Alternatives considered

- **New `tier2_approved` state between `validated` and `merged`.** Cleaner conceptually
  but requires a schema migration and changes the TRANSITIONS table. Deferred in favour of
  the flag approach which achieves the same audit trail via the `details` JSON column.
- **Policy file in the sandbox repo.** Would allow per-project policies but requires the
  orchestrator to resolve the sandbox repo path at startup. Deferred; the single global
  policy covers Phase 3 scope.
- **First-match vs max-wins for multi-path tasks.** First-match would produce inconsistent
  results depending on output path ordering. Max-wins is deterministic and conservative:
  the highest-risk path determines the whole task's tier.
