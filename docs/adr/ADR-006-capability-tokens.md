# ADR-006: Capability Tokens for Gateway Authorization

**Status:** Accepted
**Date:** 2026-07-19

## Context

The Phase 1/2 gateway checked every incoming call by querying Postgres:
"does a Run row exist for this (agent_id, task_id) and is the Task still running?"
This is a soft allowlist. Any process that knows a valid (agent_id, task_id) pair and
has network access to the gateway can impersonate a legitimate agent. There is no
cryptographic proof that the caller is the agent that was assigned the task, and no
scope enforcement beyond the repo-root path escape check.

Two concrete gaps this creates:

1. **No identity proof.** The gateway trusts `agent_id` in the request body. An agent (or
   a process that compromised the agent) can claim to be any agent on any running task.
2. **No write-path scope.** Agents can write to any path within the repo root, even paths
   outside their declared `task.outputs`.

## Decision

Introduce signed HS256 JWTs (capability tokens) minted by the orchestrator at run
creation and verified by the gateway on every call that reaches `check_active_run`.

**Token claims:**
```json
{
  "run_id":      "<uuid>",
  "task_id":     "TASK-001",
  "agent_id":    "backend-agent",
  "write_scope": ["app/main.py", "tests/"],
  "iat":         1234567890,
  "exp":         1234567890
}
```

**Expiry:** `wall_clock_min` (from task budget) + 30-minute grace, capped at 24 hours.

**Transmission:** `Authorization: Bearer <token>` header on all gateway calls.
The context packager embeds the token in `pkg["capability_token"]`; agents read it on
startup and attach it to every request.

**Scope enforcement:** `write_artifact` rejects paths not covered by `write_scope`.
A path matches if it equals a scope entry exactly or begins with `<entry>/`.
`read_artifact` is not scope-enforced (agents need broad read access to navigate unfamiliar
codebases; enforcing read scope would break legitimate exploration).

**DB check retained:** `check_active_run` is kept alongside the token as belt-and-suspenders.
The token proves identity; the DB check proves the run is still live. Both must pass.

**Opt-in deployment:** verification is skipped when `CAPABILITY_SECRET` is not set in the
environment. This allows phased deployment without breaking existing installations.

## Consequences

- An agent that does not hold the token for its run cannot call the gateway, even if it
  knows a valid (agent_id, task_id) pair.
- An agent cannot write outside its declared `task.outputs` scope (when the token carries
  a non-empty `write_scope`).
- The `CAPABILITY_SECRET` shared secret must be present on both the orchestrator host
  (minting) and the gateway host (verification). Key rotation requires restarting both.
- Tokens that reach the `exp` claim expire. Long-running tasks must be given a realistic
  `wall_clock_min` budget or the gateway will reject calls near the end of the run.
- Revocation (e.g., on human task cancellation) is not implemented in Phase 3. An agent
  whose task is cancelled can still call the gateway until the token expires. The DB check
  stops it immediately, but a token stolen mid-run could replay for up to the grace period.
  Redis-based revocation list is deferred to Phase 4.

## Alternatives considered

- **ES256 (asymmetric):** Better key hygiene — the gateway holds only the public key.
  Adds key-pair generation complexity with no meaningful security gain in Phase 3
  where both services run on the same host.
- **PASETO v4 local:** Slightly simpler API than JWT, no algorithm-confusion attacks.
  PyJWT is already a common dependency and HS256 with a strong secret is safe when the
  gateway does not accept tokens it did not configure.
- **Removing the DB check:** Token-only auth is simpler. Deferred to Phase 4 once
  token revocation is in place. Phase 3 keeps both layers.
