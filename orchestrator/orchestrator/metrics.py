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
