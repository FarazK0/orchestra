"""Root agent -- persistent daemon that accepts change requests and dispatches sub-agents.

The root agent subscribes to the ``root:requests`` Redis stream.  When a change
request arrives (submitted via ``orchctl request``), it:

  1. Iteratively explores the managed repo to build focused planning context.
  2. Calls the planner (claude CLI or LLM, depending on AGENT_TYPE) to decompose
     the change into tasks.
  3. Creates the tasks in the orchestrator.
  4. Transitions root tasks (no depends_on) to ``assigned`` so the dispatcher
     picks them up immediately.

Merges are left to ``orchctl review`` (human approval gate, unchanged).

Usage:
    uv run python -m agents.root.main \\
        [--repo PATH] [--orchestrator-url URL] [--redis-url URL]
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv

from agents.planner.plan_utils import (
    CHANGE_REQUEST_SYSTEM_PROMPT,
    build_snapshot,
    parse_task_plan,
    topo_sort,
)
from agents.shared.llm import LLMClient
from orchestrator.orchestrator.db import get_engine, get_session_factory
from orchestrator.orchestrator.streams import ROOT_STREAM_KEY, StreamConsumer, StreamPublisher

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = typer.Typer(name="root-agent", add_completion=False)

# ---------------------------------------------------------------------------
# Project state helpers
# ---------------------------------------------------------------------------


def _list_repo_files(repo_path: Path) -> list[str]:
    """Return sorted list of all repo files, excluding build artifacts."""
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
        return sorted(result.stdout.splitlines()) if result.returncode == 0 else []
    except Exception:
        return []


def _discover_context(
    repo_path: Path,
    description: str,
    agent_type: str,
    max_iterations: int = 3,
) -> tuple[str, list[str]]:
    """Iterative LLM-based file discovery before planning.

    Shows the full file tree to the LLM, which requests specific files to read.
    Returns (snapshot_string, list_of_read_paths). Falls back to build_snapshot()
    on empty repo or LLM failure so the request always continues.
    """
    all_files = _list_repo_files(repo_path)

    if not all_files:
        log.info("Discovery loop: no files found, skipping (fresh repo or empty)")
        return build_snapshot(repo_path), []

    read_files: dict[str, str] = {}

    for iteration in range(max_iterations):
        file_tree_str = "\n".join(all_files)
        already_read_str = ""
        if read_files:
            parts = [f"### {path}\n```\n{content}\n```" for path, content in read_files.items()]
            already_read_str = "\n\n".join(parts)

        prompt = (
            f"You are exploring a codebase before planning a change.\n"
            f"Change request: {description}\n\n"
            f"Complete file tree:\n{file_tree_str}\n\n"
        )
        if already_read_str:
            prompt += f"Files you have read so far:\n{already_read_str}\n\n"

        prompt += (
            "Choose ONE action (return ONLY the JSON, no explanation):\n"
            '  {"action": "read", "files": ["<path>", ...]}   <- up to 5 paths from the tree\n'
            '  {"action": "done"}\n\n'
            "Stop when you have enough context to write specific output file paths."
        )

        raw = ""
        try:
            use_api = agent_type == "python" and os.getenv("ANTHROPIC_API_KEY", "").strip()
            if use_api:
                llm = LLMClient()
                response = llm.call(
                    messages=[{"role": "user", "content": prompt}],
                    system="You are a codebase navigator. Return ONLY JSON.",
                    run_id=None,
                    session=None,
                    max_tokens=200,
                )
                raw = response.content[0].text
            else:
                claude_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
                proc = subprocess.run(
                    ["claude", "--dangerously-skip-permissions", "-p", "-"],
                    input=prompt,
                    env=claude_env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if proc.returncode != 0:
                    log.warning(
                        "Discovery iteration %d: claude exited %d; stopping",
                        iteration + 1,
                        proc.returncode,
                    )
                    break
                raw = proc.stdout
        except Exception as exc:
            log.warning("Discovery iteration %d failed: %s", iteration + 1, exc)
            break

        # Parse the LLM response — strip fences and find the JSON object.
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
        raw = raw.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            log.warning("Discovery iteration %d: no JSON object found; stopping", iteration + 1)
            break
        try:
            action_obj = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            log.warning("Discovery iteration %d: JSON parse error; stopping", iteration + 1)
            break

        action = action_obj.get("action", "")
        if action == "done":
            log.info(
                "Discovery done after %d iteration(s); read %d file(s)",
                iteration + 1,
                len(read_files),
            )
            break
        elif action == "read":
            requested = action_obj.get("files", [])[:5]
            log.info("Discovery iteration %d: requesting %s", iteration + 1, requested)
            for rel_path in requested:
                clean = rel_path.lstrip("./")
                abs_path = repo_path / clean
                try:
                    content = abs_path.read_text(encoding="utf-8")
                    read_files[clean] = content[:4000]
                except Exception:
                    read_files[clean] = "(could not read)"
        else:
            log.warning(
                "Discovery iteration %d: unknown action %r; stopping", iteration + 1, action
            )
            break

    # Build snapshot string.
    lines: list[str] = []

    try:
        git_result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if git_result.returncode == 0 and git_result.stdout.strip():
            lines.append("## Recent git history")
            lines.append(git_result.stdout.strip())
    except Exception:
        pass

    if read_files:
        lines.append("\n## Files read during exploration")
        for path, content in read_files.items():
            lines.append(f"### {path}\n```\n{content}\n```")

    remaining = [f for f in all_files if f.lstrip("./") not in read_files]
    if remaining:
        lines.append("\n## All other files (paths only)")
        lines.append("\n".join(remaining))
    else:
        lines.append("\n## Current file tree")
        lines.append("\n".join(all_files))

    snapshot = "\n".join(lines) if lines else "(project state unavailable)"
    return snapshot, list(read_files.keys())


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _fetch_agent_capabilities(orch_url: str) -> str:
    """Return a capabilities summary injected into the planner prompt.

    Reads skill memories with key='skill/capabilities' for every known agent
    identity so the planner can route infra/devops tasks to proven identities
    instead of defaulting to owner=human.
    """
    try:
        with httpx.Client(base_url=orch_url, timeout=10.0) as client:
            rows = client.get(
                "/agent-memories",
                params={"memory_type": "skill"},
            ).json()
    except Exception as exc:
        log.warning("Could not fetch agent capability memories: %s", exc)
        return ""

    if not isinstance(rows, list):
        return ""

    caps = [r for r in rows if r.get("key") == "skill/capabilities"]
    if not caps:
        return ""

    lines = ["## Agent capabilities (use to assign owner instead of 'human')"]
    for r in caps:
        agent_id = r.get("agent_id", "?")
        content = r.get("content", "").strip()
        lines.append(f"  {agent_id}: {content}")
    return "\n".join(lines)


def _fetch_existing_tasks(orch_url: str) -> str:
    """Return a two-section summary of tasks: in-flight (paths locked) and landed."""
    _IN_FLIGHT = {"created", "assigned", "running", "completed", "validated"}
    _LANDED = {"merged", "closed"}

    try:
        with httpx.Client(base_url=orch_url, timeout=10.0) as client:
            tasks = client.get("/tasks").json()
    except Exception as exc:
        log.warning("Could not fetch existing tasks: %s", exc)
        return ""

    if not isinstance(tasks, list) or not tasks:
        return ""

    in_flight_lines: list[str] = []
    landed_lines: list[str] = []
    for t in tasks:
        status = t.get("status", "")
        if status not in _IN_FLIGHT and status not in _LANDED:
            continue
        outputs = t.get("outputs") or []
        outputs_str = ", ".join(outputs[:4])
        suffix = f"  ->  {outputs_str}" if outputs_str else ""
        entry = f"  [{t['id']}] ({status})  {t['title']}{suffix}"
        if status in _IN_FLIGHT:
            in_flight_lines.append(entry)
        else:
            landed_lines.append(entry)

    if not in_flight_lines and not landed_lines:
        return ""

    sections: list[str] = []
    if in_flight_lines:
        sections.append(
            "## In-flight tasks — output paths currently locked"
            " (do not assign new tasks to these paths)\n" + "\n".join(in_flight_lines)
        )
    if landed_lines:
        sections.append(
            "## Completed/landed tasks — for context only (paths are available for new work)\n"
            + "\n".join(landed_lines)
        )
    return "\n\n".join(sections)


def _decompose_with_claude(
    description: str,
    spec_content: str,
    snapshot: str,
    existing_tasks: str,
    capabilities: str = "",
) -> str:
    """Call the claude CLI to decompose a change request. Returns raw text."""
    prompt = f"{CHANGE_REQUEST_SYSTEM_PROMPT}\n\n## Project state\n\n{snapshot}\n\n"
    if capabilities:
        prompt += f"\n{capabilities}\n\n"
    if existing_tasks:
        prompt += f"\n{existing_tasks}\n\n"
    prompt += f"## Change request\n\n{description}\n"
    if spec_content:
        prompt += f"\n## Additional spec\n\n{spec_content}\n"

    claude_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", "-"],
        input=prompt,
        env=claude_env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr[:500]}")
    return result.stdout


def _decompose_with_llm(
    description: str,
    spec_content: str,
    snapshot: str,
    existing_tasks: str,
    capabilities: str = "",
) -> str:
    """Call the LLM API to decompose a change request. Returns raw text."""
    user_content = f"## Project state\n\n{snapshot}\n\n"
    if capabilities:
        user_content += f"\n{capabilities}\n\n"
    if existing_tasks:
        user_content += f"\n{existing_tasks}\n\n"
    user_content += f"## Change request\n\n{description}\n"
    if spec_content:
        user_content += f"\n## Additional spec\n\n{spec_content}\n"

    llm = LLMClient()
    response = llm.call(
        messages=[{"role": "user", "content": user_content}],
        system=CHANGE_REQUEST_SYSTEM_PROMPT,
        run_id=None,
        session=None,
        max_tokens=2048,
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Identity memory seeding
# ---------------------------------------------------------------------------


_ROLE_DESCRIPTIONS: dict[str, str] = {
    "claude-code-agent": (
        "You are the generalist engineer for this project. "
        "You read existing code before writing, follow established patterns, and keep changes minimal. "
        "You handle any layer (backend, frontend, tests) as needed per task."
    ),
    "backend-agent": (
        "You are the backend specialist for this project. "
        "You own API routes, DB models, and business logic. "
        "You do not touch frontend files."
    ),
    "frontend-agent": (
        "You are the frontend specialist for this project. "
        "You own UI components, styles, and client-side logic. "
        "You do not touch backend or database files."
    ),
    "qa-agent": (
        "You are the QA specialist for this project. "
        "You write tests, check acceptance criteria, and verify correctness. "
        "You do not introduce new features — only validate existing ones."
    ),
}


def _role_for(agent_id: str) -> str:
    return _ROLE_DESCRIPTIONS.get(
        agent_id, f"You are a specialist agent ({agent_id}) for this project."
    )


def _seed_identity(
    agent_id: str,
    snapshot: str,
    description: str,
    orch_url: str,
    gateway_url: str,
    sample_task_id: str,
) -> None:
    """Seed or refresh identity memory for *agent_id* via the gateway.

    Skips if identity memory already exists and was updated after the
    10th-most-recent completed task for that agent (not yet stale).
    """
    # Check staleness: does identity exist and is it fresh?
    try:
        with httpx.Client(base_url=orch_url, timeout=10.0) as client:
            resp = client.get(
                "/agent-memories",
                params={"agent_id": agent_id, "memory_type": "identity"},
            )
            existing = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        log.warning("Could not check identity memory for %s: %s", agent_id, exc)
        existing = []

    if existing:
        updated_at = existing[0].get("updated_at", "")
        try:
            with httpx.Client(base_url=orch_url, timeout=10.0) as client:
                completed = client.get("/tasks", params={"status": "closed"}).json()
            agent_completed = [
                t
                for t in completed
                if t.get("owner") == agent_id and t.get("updated_at", "") > updated_at
            ]
            if len(agent_completed) < 10:
                log.debug(
                    "Identity memory for %s is fresh (%d tasks since last seed)",
                    agent_id,
                    len(agent_completed),
                )
                return
        except Exception as exc:
            log.warning("Staleness check failed for %s: %s", agent_id, exc)
            return

    # Preserve accumulated domain expertise from existing identity if present.
    expertise = "(none yet — updated as tasks complete)"
    if existing:
        old_content = existing[0].get("content", "")
        if "## Domain expertise" in old_content:
            after = old_content.split("## Domain expertise", 1)[1]
            next_sec = after.find("\n## ")
            raw = after[:next_sec].strip() if next_sec != -1 else after.strip()
            if raw and raw != "(none yet — updated as tasks complete)":
                expertise = raw

    content = (
        f"## Role\n{_role_for(agent_id)}\n\n"
        f"## Domain expertise\n{expertise}\n\n"
        f"## Project snapshot\n{snapshot[:600]}"
    )[:2000]

    try:
        with httpx.Client(base_url=gateway_url, timeout=10.0) as client:
            resp = client.post(
                "/memory/upsert",
                json={
                    "task_id": sample_task_id,
                    "agent_id": agent_id,
                    "project_id": "default",
                    "memory_type": "identity",
                    "key": "identity",
                    "content": content,
                },
                headers={"X-Platform-Actor": "root-agent"},
            )
            resp.raise_for_status()
        log.info("Seeded identity memory for %s", agent_id)
    except Exception as exc:
        log.warning("Failed to seed identity memory for %s: %s", agent_id, exc)


def _seed_shared_conventions(
    snapshot: str,
    description: str,
    gateway_url: str,
    sample_task_id: str,
) -> None:
    """Write project-level conventions into the shared memory pool (agent_id='shared').

    These are injected into every agent's context package under 'shared_skills',
    regardless of their type. Called once per change request.
    """
    content = (
        "## Project conventions\n"
        "- Follow existing file structure and naming conventions before introducing new ones.\n"
        "- Run `ruff check .` and `pytest` before calling task_complete.\n"
        "- Keep changes minimal and scoped to the task's output files.\n"
        "- Commit messages use the [TASK-ID] prefix provided in agent_instructions.\n\n"
        f"## Project snapshot\n{snapshot[:600]}\n\n"
        f"## Current change request\n{description[:300]}"
    )[:2000]

    try:
        with httpx.Client(base_url=gateway_url, timeout=10.0) as client:
            resp = client.post(
                "/memory/upsert",
                json={
                    "task_id": sample_task_id,
                    "agent_id": "shared",
                    "project_id": "default",
                    "memory_type": "convention",
                    "key": "shared/project-conventions",
                    "content": content,
                },
                headers={"X-Platform-Actor": "root-agent"},
            )
            resp.raise_for_status()
        log.info("Seeded shared project conventions")
    except Exception as exc:
        log.warning("Failed to seed shared conventions: %s", exc)


# ---------------------------------------------------------------------------
# Human task manifest
# ---------------------------------------------------------------------------


def _write_human_manifest(task_def: dict, task_id: str, repo_path: Path) -> None:
    """Write a manifest template for a human task so the human knows what to fill in.

    The manifest lives at human-gate/<slug>/manifest.json in the repo. The human
    fills in the empty values and runs `orchctl human-done` to complete the task.
    """
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", task_def["title"].lower()).strip("-")[:50]
    manifest_path = repo_path / "human-gate" / slug / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # Parse acceptance items: "KEY_NAME: description" → extract key + hint
    values: dict[str, str] = {}
    key_hints: dict[str, str] = {}
    for item in task_def.get("acceptance") or []:
        if ":" in item:
            key, _, hint = item.partition(":")
            key = key.strip().replace(" ", "_").upper()
            if key:
                values[key] = ""
                key_hints[key] = hint.strip()

    # Build step list from acceptance items that don't look like KEY: desc pairs
    steps = [a for a in (task_def.get("acceptance") or []) if ":" not in a or a.startswith("Run")]

    manifest = {
        "task_id": task_id,
        "title": task_def["title"],
        "instructions": (
            f"Complete the steps below, then fill in the values and run:\n"
            f"  orchctl human-done {task_id} --repo <repo-path>"
        ),
        "steps": steps if steps else [a for a in (task_def.get("acceptance") or [])],
        "key_hints": key_hints,
        "values": values,
    }

    import json

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log.info("Human manifest template written: %s", manifest_path)


# ---------------------------------------------------------------------------
# Task splitting helpers
# ---------------------------------------------------------------------------

_MAX_OUTPUTS: int = int(os.getenv("ORCHESTRA_MAX_OUTPUTS_BEFORE_REVIEW", "5"))

_SPLIT_SYSTEM = (
    "You are a task planning assistant. Split an oversized software task into exactly "
    "two smaller, semantically coherent sub-tasks. Return ONLY a JSON object — no "
    "markdown fences, no commentary."
)

_SPLIT_USER_TMPL = """\
Split this oversized task into exactly 2 sub-tasks.

Original task:
  title: {title}
  owner: {owner}
  outputs: {outputs}
  acceptance: {acceptance}

Rules:
- Each group should have at most {max_outputs} outputs.
- Divide the outputs between group_a and group_b by semantic theme (e.g. model vs tests).
- Divide acceptance criteria proportionally.
- Set b_depends_on_a=true if group_b's work logically requires group_a's outputs first.

Return JSON (no fences):
{{
  "group_a": {{"title": "<title>", "outputs": ["..."], "acceptance": ["..."]}},
  "group_b": {{"title": "<title>", "outputs": ["..."], "acceptance": ["..."]}},
  "b_depends_on_a": true
}}"""


def _split_task(
    task: dict,
    llm_client: LLMClient,
    max_outputs: int,
    depth: int = 0,
) -> list[dict]:
    """Recursively binary-split a task until all leaves have <= max_outputs outputs."""
    if len(task.get("outputs") or []) <= max_outputs or depth >= 4:
        return [task]

    prompt = _SPLIT_USER_TMPL.format(
        title=task["title"],
        owner=task.get("owner", ""),
        outputs=json.dumps(task.get("outputs") or []),
        acceptance=json.dumps(task.get("acceptance") or []),
        max_outputs=max_outputs,
    )
    try:
        resp = llm_client.call(
            messages=[{"role": "user", "content": prompt}],
            system=_SPLIT_SYSTEM,
            max_tokens=1024,
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if the model emits them despite instructions.
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        split = json.loads(raw)
        group_a = split["group_a"]
        group_b = split["group_b"]
        b_depends_on_a: bool = bool(split.get("b_depends_on_a", False))
    except Exception as exc:
        log.warning(
            "_split_task: LLM split failed for %r (depth=%d): %s; keeping as-is",
            task["title"],
            depth,
            exc,
        )
        return [task]

    parent_deps = list(task.get("depends_on") or [])
    sub_a: dict = {
        "title": group_a["title"],
        "owner": task.get("owner", ""),
        "depends_on": parent_deps,
        "inputs": list(task.get("inputs") or []),
        "outputs": group_a.get("outputs") or [],
        "acceptance": group_a.get("acceptance") or [],
    }
    sub_b: dict = {
        "title": group_b["title"],
        "owner": task.get("owner", ""),
        "depends_on": parent_deps + ([sub_a["title"]] if b_depends_on_a else []),
        "inputs": list(task.get("inputs") or []),
        "outputs": group_b.get("outputs") or [],
        "acceptance": group_b.get("acceptance") or [],
    }
    return _split_task(sub_a, llm_client, max_outputs, depth + 1) + _split_task(
        sub_b, llm_client, max_outputs, depth + 1
    )


def _expand_plan(
    plan: list[dict], llm_client: LLMClient | None
) -> tuple[list[dict], dict[str, list[str]]]:
    """Auto-split oversized tasks and rewrite depends_on references.

    Returns (expanded_plan, split_leaves_map) where split_leaves_map maps each
    original oversized task title to the list of leaf task titles that replaced it.
    """
    if llm_client is None:
        return plan, {}

    expanded: list[dict] = []
    split_leaves: dict[str, list[str]] = {}

    for t in plan:
        if len(t.get("outputs") or []) > _MAX_OUTPUTS:
            log.info(
                "Task %r has %d outputs (> %d) — recursively splitting",
                t["title"],
                len(t.get("outputs", [])),
                _MAX_OUTPUTS,
            )
            leaves = _split_task(t, llm_client, _MAX_OUTPUTS)
            split_leaves[t["title"]] = [s["title"] for s in leaves]
            expanded.extend(leaves)
        else:
            expanded.append(t)

    # Rewrite depends_on references that pointed at a split task.
    for t in expanded:
        new_deps: list[str] = []
        for dep in t.get("depends_on") or []:
            new_deps.extend(split_leaves.get(dep, [dep]))
        t["depends_on"] = new_deps

    return expanded, split_leaves


# ---------------------------------------------------------------------------
# Task submission
# ---------------------------------------------------------------------------


_CROSS_CUTTING_PATTERNS = [
    "docker-compose",
    ".env",
    "Makefile",
    "tailwind.config",
    "next.config",
    "postcss.config",
    "tsconfig.json",
]


def _submit_tasks(
    plan: list[dict],
    orch_url: str,
    change_id: str,
    gateway_url: str = "http://localhost:8081",
    snapshot: str = "",
    description: str = "",
    repo_path: Path | None = None,
    llm_client: LLMClient | None = None,
) -> list[str]:
    """Create tasks, seed identity memory for each agent type, approve roots."""
    plan, _ = _expand_plan(plan, llm_client)
    ordered = topo_sort(plan)
    title_to_id: dict[str, str] = {}

    # Coverage gap warning: cross-cutting files mentioned in description but owned by no task.
    all_outputs = {p for t in plan for p in (t.get("outputs") or [])}
    uncovered = [
        pat
        for pat in _CROSS_CUTTING_PATTERNS
        if pat in (description or "") and not any(pat in o or o in pat for o in all_outputs)
    ]
    if uncovered:
        log.warning(
            "Plan coverage gap — description mentions %s but no task owns them; "
            "agents may write out-of-scope and get rejected",
            uncovered,
        )

    # Breadth + acceptance check (output count is handled by _expand_plan auto-split).
    repo_files = _list_repo_files(repo_path) if repo_path else []
    oversized: list[str] = []
    for t in plan:
        too_many_criteria = len(t.get("acceptance") or []) > 15
        broad_dir = False
        for output in t.get("outputs", []):
            clean = output.lstrip("./")
            if output.endswith("/") and clean and repo_files:
                count = sum(1 for f in repo_files if f.lstrip("./").startswith(clean))
                if count > 15:
                    broad_dir = True
                    log.warning(
                        "Task %r: output %r covers %d existing files — flagging for approval",
                        t["title"],
                        output,
                        count,
                    )
        if too_many_criteria or broad_dir:
            oversized.append(t["title"])

    needs_approval = bool(oversized)
    if needs_approval:
        log.warning(
            "PLAN REQUIRES HUMAN REVIEW — the following tasks may be over-scoped "
            "and have been held in 'created' state. Review with `orchctl list` then run:\n"
            "  orchctl approve TASK-NNN   # repeat for each task you want to start\n"
            "Oversized tasks flagged: %s",
            oversized,
        )

    with httpx.Client(base_url=orch_url, timeout=15.0) as client:
        for task_def in ordered:
            depends_on_ids = [
                title_to_id[t] for t in task_def.get("depends_on", []) if t in title_to_id
            ]
            payload = {
                "title": task_def["title"],
                "owner": task_def["owner"],
                "depends_on": depends_on_ids,
                "inputs": task_def.get("inputs", []),
                "outputs": task_def.get("outputs", []),
                "acceptance": task_def.get("acceptance", []),
                "risk_tier": 1,
                "project_path": str(repo_path) if repo_path else None,
            }
            resp = client.post("/tasks", json=payload)
            resp.raise_for_status()
            created = resp.json()
            title_to_id[task_def["title"]] = created["id"]
            log.info("Created %s: %r [%s]", created["id"], created["title"], created["owner"])

            # For human tasks: write the manifest template to the repo so the human
            # knows exactly what values to fill in before running orchctl human-done.
            if task_def["owner"] == "human" and repo_path:
                _write_human_manifest(task_def, created["id"], repo_path)

        if needs_approval:
            # Leave all tasks in 'created' — human must orchctl approve each one.
            pass
        else:
            # Approve root tasks (no depends_on) — dispatcher will launch them immediately.
            for task_def in ordered:
                if task_def.get("depends_on"):
                    continue
                task_id = title_to_id[task_def["title"]]
                resp = client.post(
                    f"/tasks/{task_id}/transition",
                    json={
                        "new_status": "assigned",
                        "actor": "root-agent",
                        "payload": {"change_id": change_id},
                    },
                )
                resp.raise_for_status()
                log.info("Approved %s -> assigned", task_id)

    # Seed identity memory for each unique agent type in the plan (skip human — no LLM loop).
    seen_agents: set[str] = set()
    task_ids = list(title_to_id.values())
    sample_task_id_for_shared = task_ids[0] if task_ids else ""
    for task_def in ordered:
        agent_id = task_def["owner"]
        if agent_id in seen_agents or agent_id == "human":
            continue
        seen_agents.add(agent_id)
        sample_task_id = title_to_id[task_def["title"]]
        _seed_identity(agent_id, snapshot, description, orch_url, gateway_url, sample_task_id)

    # Seed shared project conventions for all agents.
    if sample_task_id_for_shared:
        _seed_shared_conventions(snapshot, description, gateway_url, sample_task_id_for_shared)

    return task_ids


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


class RootAgent:
    def __init__(
        self,
        repo_path: Path,
        orch_url: str,
        session_factory,
        redis_url: str | None = None,
        gateway_url: str = "http://localhost:8081",
    ) -> None:
        self._repo = repo_path
        self._orch_url = orch_url
        self._gateway_url = gateway_url
        self._redis_url = redis_url
        self._consumer = StreamConsumer(
            consumer_group="root-agent",
            consumer_name="root-0",
            session_factory=session_factory,
            redis_url=redis_url,
            stream_key=ROOT_STREAM_KEY,
        )
        self._root_publisher: StreamPublisher | None = None
        self._agent_type = os.getenv("AGENT_TYPE", "claude-code")
        self._tick = 0

    # ------------------------------------------------------------------
    # Planner re-entry (Stage 5)
    # ------------------------------------------------------------------

    def _handle_replan(self, payload: dict) -> None:
        """Re-evaluate the task plan after a task discovery event.

        Fetches all pending tasks, builds a focused prompt describing the
        newly discovered work, calls the planner, and submits any additional
        tasks the planner decides are necessary.  Emits PLAN_UPDATED when
        at least one new task is added.
        """
        trigger_task_id = payload.get("trigger_task_id", "")
        child_task_id = payload.get("child_task_id", "")
        reason = payload.get("reason", "")

        log.info("Replan: trigger=%s child=%s", trigger_task_id, child_task_id)

        # 1. Fetch current task list.
        try:
            with httpx.Client(base_url=self._orch_url, timeout=10.0) as client:
                all_tasks = client.get("/tasks").json()
        except Exception as exc:
            log.error("Replan: could not fetch tasks: %s", exc)
            return

        # 2. Only bother replanning if there are other pending tasks besides the child.
        pending_statuses = {"created", "assigned"}
        other_pending = [
            t
            for t in all_tasks
            if t.get("status") in pending_statuses and t.get("id") != child_task_id
        ]
        if not other_pending:
            log.info("Replan: no other pending tasks; nothing to replan")
            return

        # 3. Build a focused prompt asking whether any plan adjustments are needed.
        pending_summary = "\n".join(
            f"  [{t['id']}] ({t['status']}) {t['title']}" for t in other_pending
        )
        replan_description = (
            f"A new task was discovered during execution.\n"
            f"Reason: {reason}\n"
            f"New child task ID: {child_task_id}\n"
            f"Parent task ID: {trigger_task_id}\n\n"
            f"Pending tasks that may be affected:\n{pending_summary}\n\n"
            "If any of these pending tasks should now depend on the new child task, "
            "or if new supporting tasks must be added, output them in the standard "
            "JSON plan format.  Only output truly necessary additions or adjustments. "
            "If no changes are needed, output an empty JSON array []."
        )

        snapshot = build_snapshot(self._repo)
        existing = _fetch_existing_tasks(self._orch_url)
        capabilities = _fetch_agent_capabilities(self._orch_url)
        use_api = self._agent_type == "python" and os.getenv("ANTHROPIC_API_KEY", "").strip()

        try:
            if use_api:
                raw = _decompose_with_llm(replan_description, "", snapshot, existing, capabilities)
            else:
                raw = _decompose_with_claude(
                    replan_description, "", snapshot, existing, capabilities
                )
        except Exception as exc:
            log.warning("Replan: decomposition failed: %s", exc)
            return

        # 4. Parse and submit any new tasks.
        try:
            plan = parse_task_plan(raw)
        except (json.JSONDecodeError, IndexError, ValueError):
            log.info("Replan: planner returned no parseable plan; no changes made")
            return

        if not plan:
            log.info("Replan: planner decided no changes are needed")
            return

        log.info("Replan: adding %d new task(s)", len(plan))
        change_id = str(uuid.uuid4())
        _replan_llm = LLMClient() if use_api else None
        try:
            new_task_ids = _submit_tasks(
                plan,
                self._orch_url,
                change_id,
                gateway_url=self._gateway_url,
                snapshot=snapshot,
                description=replan_description,
                repo_path=self._repo,
                llm_client=_replan_llm,
            )
        except Exception as exc:
            log.error("Replan: task submission failed: %s", exc)
            return

        # 5. Emit PLAN_UPDATED to root stream so any observer can track the change.
        if self._root_publisher is None:
            self._root_publisher = StreamPublisher(self._redis_url)
        try:
            self._root_publisher.publish(
                str(uuid.uuid4()),
                "PLAN_UPDATED",
                trigger_task_id,
                {
                    "trigger_task_id": trigger_task_id,
                    "child_task_id": child_task_id,
                    "new_task_ids": new_task_ids,
                    "reason": reason,
                },
                stream_key=ROOT_STREAM_KEY,
            )
            log.info("PLAN_UPDATED emitted: %d new task(s) added", len(new_task_ids))
        except Exception as exc:
            log.warning("Could not emit PLAN_UPDATED: %s", exc)

    def start(self) -> None:
        self._consumer.ensure_group()
        log.info(
            "Root agent started on stream %s (agent_type=%s)", ROOT_STREAM_KEY, self._agent_type
        )
        while True:
            self._consumer.reclaim_pending()
            self._consumer.consume_one(self._handle)
            self._tick += 1

    def _handle(self, fields: dict) -> None:
        event_type = fields.get("event_type", "")

        if event_type == "PLAN_REPLAN_REQUESTED":
            try:
                payload = json.loads(fields.get("payload", "{}"))
            except json.JSONDecodeError:
                log.warning("Bad payload JSON in PLAN_REPLAN_REQUESTED")
                return
            self._handle_replan(payload)
            return

        if event_type != "CHANGE_REQUEST":
            log.debug("Ignoring event_type=%s", event_type)
            return

        try:
            payload = json.loads(fields.get("payload", "{}"))
        except json.JSONDecodeError:
            log.warning("Bad payload JSON in CHANGE_REQUEST")
            return

        description = payload.get("description", "").strip()
        spec_path_str = payload.get("spec_path", "").strip()
        change_id = fields.get("event_id") or str(uuid.uuid4())

        if not description:
            log.warning("CHANGE_REQUEST has empty description; skipping")
            return

        log.info("Processing change request [%s]: %s", change_id, description[:80])

        # Read optional spec file from the managed repo.
        spec_content = ""
        if spec_path_str:
            spec_file = self._repo / spec_path_str
            if spec_file.is_file():
                spec_content = spec_file.read_text(encoding="utf-8")
                log.info("Loaded spec file: %s (%d chars)", spec_path_str, len(spec_content))
            else:
                log.warning("spec_path %r not found in repo; ignoring", spec_path_str)

        # Iterative file discovery: show full tree, LLM requests relevant files.
        snapshot, discovered_inputs = _discover_context(self._repo, description, self._agent_type)
        log.info(
            "Discovery complete: read %d file(s), snapshot %d chars",
            len(discovered_inputs),
            len(snapshot),
        )

        # Fetch existing tasks so the planner can avoid re-creating closed work.
        existing_tasks = _fetch_existing_tasks(self._orch_url)
        if existing_tasks:
            log.info("Injecting existing task context into planner (%d chars)", len(existing_tasks))

        # Fetch agent capability memories so the planner can route to proven identities.
        capabilities = _fetch_agent_capabilities(self._orch_url)
        if capabilities:
            log.info("Injecting agent capabilities into planner (%d chars)", len(capabilities))

        # Decompose into tasks.
        use_api = self._agent_type == "python" and os.getenv("ANTHROPIC_API_KEY", "").strip()
        try:
            if use_api:
                raw = _decompose_with_llm(
                    description, spec_content, snapshot, existing_tasks, capabilities
                )
            else:
                raw = _decompose_with_claude(
                    description, spec_content, snapshot, existing_tasks, capabilities
                )
        except Exception as exc:
            log.error("Decomposition failed for change [%s]: %s", change_id, exc)
            return

        try:
            plan = parse_task_plan(raw)
        except (json.JSONDecodeError, IndexError, ValueError) as exc:
            log.error("Could not parse task plan for [%s]: %s\nRaw: %s", change_id, exc, raw[:500])
            return

        if not isinstance(plan, list) or not plan:
            log.error("Empty task plan for change [%s]; raw: %s", change_id, raw[:200])
            return

        log.info("Plan for [%s]: %d tasks", change_id, len(plan))

        # Merge discovery-read files into every task's inputs (Fix 5).
        if discovered_inputs:
            for task_def in plan:
                existing_inputs = set(task_def.get("inputs") or [])
                task_def["inputs"] = sorted(existing_inputs | set(discovered_inputs))
            log.info("Merged %d discovery file(s) into task inputs", len(discovered_inputs))

        try:
            task_ids = _submit_tasks(
                plan,
                self._orch_url,
                change_id,
                gateway_url=self._gateway_url,
                snapshot=snapshot,
                description=description,
                repo_path=self._repo,
                llm_client=LLMClient() if use_api else None,
            )
            log.info("Dispatched %d tasks for change [%s]: %s", len(task_ids), change_id, task_ids)
        except Exception as exc:
            log.error("Task submission failed for change [%s]: %s", change_id, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@app.command()
def main(
    repo: str = typer.Option(None, "--repo", "-r", help="Managed repo path."),
    orchestrator_url: str = typer.Option(None, "--orchestrator-url", help="Orchestrator base URL."),
    redis_url: str = typer.Option(None, "--redis-url", help="Redis URL."),
    gateway_url: str = typer.Option(None, "--gateway-url", help="Gateway base URL."),
) -> None:
    """Persistent root agent — consumes change requests and dispatches sub-agents."""
    repo_path = Path(repo or os.getenv("SANDBOX_REPO_PATH", "./sandbox/sample-project")).resolve()
    orch_url = orchestrator_url or os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")
    r_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6380")
    gw_url = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8081")
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://orchestra:orchestra@localhost:5433/orchestra",
    )

    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    agent = RootAgent(repo_path, orch_url, factory, r_url, gateway_url=gw_url)
    try:
        agent.start()
    except KeyboardInterrupt:
        log.info("Root agent stopped.")
        sys.exit(0)


if __name__ == "__main__":
    app()
