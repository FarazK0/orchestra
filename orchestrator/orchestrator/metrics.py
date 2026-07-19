"""Prometheus application metrics for the Orchestra platform.

Imported by api.py, validator.py, and agents/shared/llm.py.
The gateway exposes its own HTTP request metrics via
prometheus-fastapi-instrumentator (no custom gateway counters needed).
"""

from prometheus_client import Counter, Histogram

tasks_total = Counter(
    "orchestra_tasks_total",
    "Task lifecycle events by state transition",
    ["new_status", "owner"],
)

task_cost_usd = Counter(
    "orchestra_task_cost_usd_total",
    "Cumulative LLM cost in USD",
    ["agent_id", "model"],
)

validator_results_total = Counter(
    "orchestra_validator_results_total",
    "Validator (ruff + pytest) outcomes",
    ["result", "owner"],
)

human_queue_latency_seconds = Histogram(
    "orchestra_human_queue_latency_seconds",
    "Time from task validated to task merged (human review latency)",
    ["owner"],
    buckets=[60, 300, 900, 1800, 3600, 7200, 86400],
)

# v0.3 adaptive orchestration metrics
tasks_discovered_total = Counter(
    "orchestra_tasks_discovered_total",
    "TASK_DISCOVERED events accepted by the Scheduler (child created + parent blocked)",
)

tasks_blocked_total = Counter(
    "orchestra_tasks_blocked_total",
    "Parent tasks transitioned running → blocked",
)

tasks_resumed_total = Counter(
    "orchestra_tasks_resumed_total",
    "Parent tasks transitioned blocked → assigned after all children completed",
)

task_discovery_rejected_total = Counter(
    "orchestra_task_discovery_rejected_total",
    "TASK_DISCOVERED events rejected by the Scheduler",
    ["reason"],
)

spawn_depth_histogram = Histogram(
    "orchestra_task_spawn_depth",
    "Distribution of child task spawn depths at discovery time",
    buckets=[0, 1, 2, 3, 4, 5],
)
