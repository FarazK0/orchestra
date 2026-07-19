# ADR-007: Observability — Prometheus metrics, OTel tracing, Grafana dashboard

**Status:** Accepted  
**Date:** 2026-07-19

## Context

Phase 3 governance required an observability pass covering: task-lifecycle metrics,
LLM cost accounting, validator pass rate, human review queue latency, and distributed
traces per agent run. The demo target is a refused out-of-scope gateway write visible
in both the audit log and a distributed trace.

## Decision

**Metrics: Prometheus pull model via `prometheus-fastapi-instrumentator`.**  
Both FastAPI services expose `/metrics`. The instrumentator adds HTTP request counters
and latency histograms automatically; four application-level counters/histograms are
added on top (`orchestra_tasks_total`, `orchestra_task_cost_usd_total`,
`orchestra_validator_results_total`, `orchestra_human_queue_latency_seconds`).

**Traces: OpenTelemetry FastAPI auto-instrumentation exporting to Jaeger all-in-one.**  
`opentelemetry-instrumentation-fastapi` patches both services at startup.
Every HTTP request (including refused gateway calls) becomes a span. No manual spans
in Phase 3 — auto-instrumentation is sufficient for the demo. `OTLP_ENDPOINT` must be
set to activate; absent means no-op (backward-compatible).

**Backend: Jaeger all-in-one + Prometheus + Grafana in docker-compose.**  
Prometheus scrapes via `host.docker.internal` (resolved by `extra_hosts: host-gateway`
in WSL2). Grafana is pre-provisioned with the Prometheus datasource and a six-panel
Orchestra dashboard. Anonymous admin access enabled for local dev.

## Consequences

- **No denied-access audit rows.** A 403 from the gateway appears in the OTel trace as
  an error span (HTTP 4xx) but does not generate an `audit` table row — `write_gateway_audit`
  only runs after the operation succeeds. Full denied-access auditing is Phase 4.
- **OTel SQLAlchemy/httpx instrumentation deferred.** Would add noise in Phase 3;
  decision to add them only if query profiling or cross-service call graphs are needed.
- **Prometheus counter naming.** Python client appends `_total` suffix automatically to
  counter family names; `orchestra_tasks_total` is exposed as `orchestra_tasks_total_total`.
- **`instrument(app)` must run before the first request.** Called at module level so the
  Starlette middleware stack is not yet built; `expose(app)` is called in the lifespan
  (adds a route, not middleware — safe after startup). A `_metrics_exposed` guard prevents
  duplicate `/metrics` routes across TestClient contexts in the same test process.

## Alternatives considered

- **Pushgateway instead of pull.** Fits agents that are short-lived processes, but adds
  an extra service. Since the orchestrator and gateway are long-running, pull is simpler.
- **Tempo instead of Jaeger.** Requires a separate OTel Collector container; Jaeger
  all-in-one is a single image that handles OTLP ingestion and the UI.
- **PASETO or ES256 tokens for OTel.** Not applicable — this ADR is about observability.
  The capability token ADR is ADR-006.
