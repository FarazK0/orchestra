# ADR-003: All side effects flow through the tool gateway

Status: Accepted

## Decision
Agents hold no credentials of any kind. Every read, write, command execution,
git operation, and event emission is performed by the gateway on the agent's
behalf, after a permission check, with an audit row written atomically.

## Rationale
Makes permission enforcement structural rather than advisory: a misbehaving or
prompt-injected agent cannot perform an action the gateway refuses. Also gives
complete auditability for free.

## Consequences
The gateway is on the critical path of every operation and must be reliable and
fast. Phase 1 permission checks are allowlists; Phase 3 replaces them with signed
capability tokens without changing the agent-facing interface.
