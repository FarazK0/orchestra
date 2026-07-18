"""Shared utilities for task plan parsing and topological sorting.

Used by both agents.planner.main (one-shot planner) and agents.root.main
(persistent root agent) so the logic stays in one place.
"""

from __future__ import annotations

import json
import re


_TASK_JSON_SCHEMA = """\
Return ONLY a JSON array -- no explanation, no markdown code fences. Each element:
{
  "title":      "<short imperative phrase>",
  "owner":      "<agent-id>",
  "depends_on": ["<exact title of another task in this list>"],
  "inputs":     ["<repo-relative file path this task reads>"],
  "outputs":    ["<repo-relative file path this task writes>"],
  "acceptance": ["<one acceptance criterion per string>"]
}
"""

PLANNER_SYSTEM_PROMPT = (
    """\
You are a project planner for the Orchestra multi-agent orchestration platform.
Read the specification below and decompose the work into tasks for these agents:

  backend-agent      -- server-side: APIs, data models, business logic, tests
  frontend-agent     -- client-side: HTML, CSS, JavaScript, single-page UI
  qa-agent           -- quality: test plans, QA reports, risk assessment
  claude-code-agent  -- general purpose: Claude Code CLI worker; handles backend,
                       frontend, or QA work; preferred when a single capable agent
                       can own the full implementation

"""
    + _TASK_JSON_SCHEMA
    + """
Rules:
- backend-agent tasks have no depends_on (they are always roots).
- frontend-agent and qa-agent tasks depend on the backend-agent task whose outputs
  they consume -- list those backend task titles in their depends_on.
- Keep the plan to 3-5 tasks total; do not split work an agent can handle internally.
- Do not include risk_tier; the planner will set it to 1 for all tasks.
"""
)

CHANGE_REQUEST_SYSTEM_PROMPT = (
    """\
You are a project planner for the Orchestra multi-agent orchestration platform.
A human has submitted a change request for an existing software project.
You will be given the current state of the project (file tree and recent git log)
and the change request.

Decompose the change into tasks for these agents:

  backend-agent      -- server-side: APIs, data models, business logic, migrations, tests
  frontend-agent     -- client-side: HTML, CSS, JavaScript, single-page UI, templates
  qa-agent           -- quality only: test plans, QA reports, risk assessment (no implementation)
  claude-code-agent  -- cross-cutting: tasks that genuinely span all layers and cannot
                       be cleanly assigned to a single specialist

"""
    + _TASK_JSON_SCHEMA
    + """
Rules:
- Use backend-agent for server-side work (APIs, DB models, business logic).
- Use frontend-agent for client-side work (HTML, CSS, JS, UI templates).
- Use qa-agent for test-only tasks that validate existing features, not implement them.
- Use claude-code-agent only for tasks that genuinely cross all layers.
- backend-agent tasks have no depends_on (they are always roots).
- frontend-agent and qa-agent tasks depend on the backend tasks whose outputs they consume.
- Root tasks (no depends_on) will be dispatched immediately.
- Downstream tasks unblock when their depends_on are all closed.
- Keep the plan to 1-5 tasks; do not over-split a change an agent can handle in one go.
- Do not re-create tasks for work already done per the existing tasks list.
- Do not include risk_tier; the planner sets it to 1 for all tasks.
"""
)

def topo_sort(tasks: list[dict]) -> list[dict]:
    """Return tasks in an order where every task appears after its dependencies."""
    by_title = {t["title"]: t for t in tasks}
    visited: set[str] = set()
    result: list[dict] = []

    def visit(t: dict) -> None:
        if t["title"] in visited:
            return
        for dep in t.get("depends_on", []):
            if dep in by_title:
                visit(by_title[dep])
        visited.add(t["title"])
        result.append(t)

    for t in tasks:
        visit(t)
    return result


def parse_task_plan(text: str) -> list[dict]:
    """Extract and parse a JSON array from text.

    Handles markdown fences and conversational preamble/postamble that models
    (including the claude CLI) may add around the JSON array.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()
    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start : end + 1]
    return json.loads(text)
