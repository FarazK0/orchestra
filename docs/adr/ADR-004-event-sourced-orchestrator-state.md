# ADR-004: Event-sourced orchestrator state

Status: Accepted

## Decision
The events table is append-only, and orchestrator state must be reconstructable
by replaying it. Consumers deduplicate on event_id (at-least-once delivery).

## Rationale
Replay gives deterministic reconstruction for debugging, a natural regression
test harness for the orchestrator, and crash recovery semantics.

## Consequences
Events are versioned (schema_version). Destructive migrations of the events
table are forbidden; corrections happen via compensating events.
