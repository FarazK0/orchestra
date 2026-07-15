# ADR-002: Postgres as the control plane

Status: Accepted

## Decision
Tasks, events, runs, capabilities, and the audit index live in Postgres.
Agents never access the database directly; only the orchestrator and gateway do.

## Rationale
Transactional guarantees for state transitions (event + audit + status change in
one transaction) and simple querying for scheduling and reporting.

## Consequences
Alembic migrations are mandatory for any schema change. Artifact content never
goes into Postgres (see ADR-001).
