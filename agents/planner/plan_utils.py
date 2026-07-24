"""Shared utilities for task plan parsing and topological sorting.

Used by both agents.planner.main (one-shot planner) and agents.root.main
(persistent root agent) so the logic stays in one place.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


_TASK_JSON_SCHEMA = """\
Return ONLY a JSON array -- no explanation, no markdown code fences. Each element:
{
  "title":      "<short imperative phrase>",
  "owner":      "<agent-id>",
  "depends_on": ["<exact title of another task in this list>"],
  "inputs":     ["<repo-relative file path this task reads>"],
  "outputs":    ["<repo-relative directory/ or file path this task exclusively owns — include root-level config files explicitly if no other task covers them (e.g. docker-compose.yml, .env.example, Makefile, next.config.ts, tailwind.config.ts, postcss.config.mjs, tsconfig.json)>"],
  "acceptance": ["<one acceptance criterion per string>"]
}
"""

CHANGE_REQUEST_SYSTEM_PROMPT = (
    """\
You are a project planner for the Orchestra multi-agent orchestration platform.
A human has submitted a change request for an existing software project.
You will be given the current state of the project (file tree and recent git log)
and the change request.

Decompose the change into tasks. Assign each task an agent identity (the "owner" field)
that reflects the domain specialisation the agent brings to the work:

  backend-agent   -- specialises in: APIs, data models, business logic, migrations, tests
  frontend-agent  -- specialises in: HTML, CSS, JavaScript, UI templates, browser interaction
  qa-agent        -- specialises in: test plans, QA reports, risk assessment (no new features)

The execution backend (which code actually runs the LLM loop) is a system-wide setting
and is NOT determined by the owner field. Assign the identity whose specialisation best
matches the task's domain outputs. For tasks that genuinely span all layers, assign the
identity of the layer that owns the most outputs, or split into separate tasks with a
depends_on relationship.

"""
    + _TASK_JSON_SCHEMA
    + """
Rules:
- Use backend-agent for server-side work (APIs, DB models, business logic, migrations).
- Use frontend-agent for client-side work (HTML, CSS, JS, UI templates).
- Use qa-agent for test-only tasks that validate existing features, not implement them.
- For cross-cutting tasks that span all layers, assign the identity that owns the
  majority of outputs. If the task would have more than 5 acceptance criteria or span
  more than one major subsystem, split it with a depends_on relationship instead.
- backend-agent tasks have no depends_on (they are always roots).
- frontend-agent and qa-agent tasks depend on the backend tasks whose outputs they consume.
- Root tasks (no depends_on) will be dispatched immediately.
- Downstream tasks unblock when their depends_on are all closed.
- Keep the plan to 1-5 tasks; do not over-split a change an agent can handle in one go.
- Do not re-create tasks for work already done per the existing tasks list.
- Do not include risk_tier; the planner sets it to 1 for all tasks.
- Do not assign to any new task's outputs a path that appears after "->" in the
  "In-flight tasks" section; those paths are owned by tasks currently in progress.
  Paths in the "Completed/landed tasks" section are available for new work.
- In outputs, prefer specific file paths over bare directories when working in an
  existing codebase (e.g. "backend/app/routers/listings.py" not "backend/"). Use a
  bare directory only when the task creates an entire new subsystem from scratch with
  no pre-existing files under it.

Coverage check (run before returning the plan):
1. List every top-level directory and cross-cutting file the change requires.
2. For each item, verify it is covered by at least one task's outputs list.
3. If any item is uncovered, assign it to the most relevant existing task's outputs
   or add a new task to cover it (use the identity that best matches the uncovered outputs).
Never return a plan where a file the change explicitly requires has no owning task.

Task size limit:
- No single task may have more than 5 acceptance criteria.
- No single task may cover more than one major subsystem.
- If a natural task would exceed these limits, split it with a depends_on relationship.
"""
)


def build_snapshot(repo_path: Path) -> str:
    """Build a compact project snapshot from git log + file tree (no LLM, no cap).

    Used by the one-shot planner (planner/main.py --spec mode) and as the fallback
    for _discover_context() when the repo is empty or the LLM is unavailable.
    """
    lines: list[str] = []

    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines.append("## Recent git history")
            lines.append(result.stdout.strip())
    except Exception:
        pass

    try:
        result = subprocess.run(
            [
                "find",
                ".",
                "-not",
                "-path",
                "./.git*",
                "-not",
                "-path",
                "./__pycache__*",
                "-not",
                "-name",
                "*.pyc",
                "-not",
                "-path",
                "./.pytest_cache*",
                "-not",
                "-path",
                "./.ruff_cache*",
                "-not",
                "-path",
                "./.venv*",
                "-not",
                "-path",
                "*/node_modules*",
                "-not",
                "-path",
                "./.next*",
                "-not",
                "-path",
                "./.orchestra*",
                "-not",
                "-path",
                "*/dist/*",
                "-not",
                "-path",
                "*/build/*",
                "-not",
                "-path",
                "*/*.egg-info*",
                "-not",
                "-path",
                "*/.mypy_cache*",
                "-not",
                "-path",
                "*/htmlcov*",
                "-not",
                "-path",
                "*/target/*",
                "-type",
                "f",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            files = sorted(result.stdout.splitlines())[:100]
            lines.append("\n## Current file tree")
            lines.append("\n".join(files))
    except Exception:
        pass

    return "\n".join(lines) if lines else "(project state unavailable)"


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
