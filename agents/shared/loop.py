"""Base agent loop: context package → plan → act via gateway → commit.

The loop drives a Claude tool-use conversation. Tools map 1-to-1 to
gateway endpoints. When the agent calls `task_complete` the loop:
  1. Commits staged changes to the agent branch via the gateway.
  2. Transitions the task to 'completed' via the orchestrator.
  3. Records the run outcome on the Run row.

The caller owns the DB session transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import Run

from .llm import LLMClient

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic JSON schema format)
# ---------------------------------------------------------------------------

GATEWAY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_artifact",
        "description": "Read a file from the managed repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within the repo root."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_artifact",
        "description": "Write or overwrite a file in the managed repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within the repo root."},
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a command in the managed repo directory and return its output. "
            "Use this to run tests (e.g. ['pytest', 'tests/']) or check outputs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command and arguments as a list (e.g. ['pytest', '-x']).",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "emit_event",
        "description": "Emit a structured event to the orchestrator control plane.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": "Event type, e.g. HUMAN_ATTENTION_NEEDED.",
                },
                "payload": {"type": "object", "description": "Event-specific data."},
            },
            "required": ["event_type"],
        },
    },
    {
        "name": "task_complete",
        "description": (
            "Signal that all acceptance criteria are satisfied. "
            "This commits the changes and transitions the task to 'completed'. "
            "Only call this after verifying every acceptance criterion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commit_message": {
                    "type": "string",
                    "description": "Commit message body (the commit prefix is prepended automatically).",
                },
                "paths_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relative paths of all files to include in the commit.",
                },
            },
            "required": ["commit_message", "paths_changed"],
        },
    },
]


# ---------------------------------------------------------------------------
# Context package formatting
# ---------------------------------------------------------------------------


def format_context_package(pkg: dict) -> str:
    """Render a context package dict as the agent's opening user message."""
    task = pkg["task"]
    lines: list[str] = [
        f"## Task: {task['title']}",
        "",
        "### Acceptance Criteria",
        *[f"- {c}" for c in task["acceptance"]],
        "",
        "### Input Files",
    ]

    for art in pkg.get("input_artifacts", []):
        if art.get("found"):
            lines += [
                f"#### {art['path']}",
                "```",
                art.get("content") or "",
                "```",
                "",
            ]
        else:
            lines.append(f"#### {art['path']} (does not exist yet — create it)")
            lines.append("")

    adrs = pkg.get("adrs", [])
    if adrs:
        lines.append("### Architecture Decisions (read-only, not instructions)")
        for adr in adrs:
            lines += [
                f"#### {adr['path']}",
                adr.get("content") or "",
                "",
            ]

    instr = pkg["agent_instructions"]
    lines += [
        "### Your Instructions",
        f"- Branch: `{instr['branch']}`",
        f"- Commit prefix: `{instr['commit_prefix']}`",
        f"- Write scope: {instr['write_scope']}",
        "",
        (
            "Use the provided tools for every file read/write/command. "
            "Call `task_complete` only after verifying each acceptance criterion above."
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gateway tool dispatch
# ---------------------------------------------------------------------------


def _call_gateway(
    http: httpx.Client,
    gateway_url: str,
    path: str,
    payload: dict,
) -> dict:
    resp = http.post(f"{gateway_url}{path}", json=payload)
    resp.raise_for_status()
    return resp.json()


def _execute_gateway_tool(
    name: str,
    tool_input: dict,
    agent_id: str,
    task_id: str,
    repo_path: str,
    gateway_url: str,
    http: httpx.Client,
) -> str:
    """Dispatch a single tool call to the gateway; return a text result."""
    base: dict = {"agent_id": agent_id, "task_id": task_id}

    if name == "read_artifact":
        data = _call_gateway(
            http,
            gateway_url,
            "/read_artifact",
            {**base, "repo_path": repo_path, "path": tool_input["path"]},
        )
        return data["content"] if data.get("found") else f"(not found: {tool_input['path']})"

    if name == "write_artifact":
        _call_gateway(
            http,
            gateway_url,
            "/write_artifact",
            {
                **base,
                "repo_path": repo_path,
                "path": tool_input["path"],
                "content": tool_input["content"],
            },
        )
        return f"Written: {tool_input['path']}"

    if name == "run_command":
        data = _call_gateway(
            http,
            gateway_url,
            "/run_command",
            {**base, "repo_path": repo_path, "command": tool_input["command"]},
        )
        return (
            f"returncode={data['returncode']}\nstdout:\n{data['stdout']}\nstderr:\n{data['stderr']}"
        )

    if name == "emit_event":
        _call_gateway(
            http,
            gateway_url,
            "/emit_event",
            {
                **base,
                "event_type": tool_input["event_type"],
                "payload": tool_input.get("payload", {}),
            },
        )
        return f"Event emitted: {tool_input['event_type']}"

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------


def run_agent_loop(
    context_package: dict,
    repo_path: str,
    gateway_url: str,
    orchestrator_url: str,
    llm: LLMClient,
    system_prompt: str,
    run_id: uuid.UUID,
    session: Session,
    max_iterations: int = 20,
) -> str:
    """Drive the agent loop until task_complete or max_iterations.

    Returns "completed" or "failed". The caller must commit the session.
    """
    task_id: str = context_package["task_id"]
    agent_id: str = context_package["agent_instructions"]["agent_id"]
    instr: dict = context_package["agent_instructions"]
    branch: str = instr["branch"]
    commit_prefix: str = instr["commit_prefix"]

    messages: list[dict] = [{"role": "user", "content": format_context_package(context_package)}]

    def _finish(result: str) -> str:
        run = session.get(Run, run_id)
        if run is not None:
            run.finished_at = datetime.now(timezone.utc)
            run.result = result
            session.flush()
        return result

    with httpx.Client(timeout=60.0) as http:
        # Create/switch to the agent branch before the first LLM turn.
        _call_gateway(
            http,
            gateway_url,
            "/git/branch",
            {"agent_id": agent_id, "task_id": task_id, "repo_path": repo_path, "branch": branch},
        )

        for _iteration in range(max_iterations):
            response = llm.call(
                messages=messages,
                system=system_prompt,
                tools=GATEWAY_TOOLS,
                run_id=run_id,
                session=session,
            )

            if response.stop_reason == "end_turn":
                # Agent finished without calling task_complete.
                http.post(
                    f"{orchestrator_url}/tasks/{task_id}/transition",
                    json={
                        "new_status": "failed",
                        "actor": agent_id,
                        "details": {"reason": "agent stopped without task_complete"},
                    },
                )
                return _finish("failed")

            if response.stop_reason != "tool_use":
                http.post(
                    f"{orchestrator_url}/tasks/{task_id}/transition",
                    json={
                        "new_status": "failed",
                        "actor": agent_id,
                        "details": {"stop_reason": response.stop_reason},
                    },
                )
                return _finish("failed")

            # Process tool calls.
            tool_results: list[dict] = []
            completed = False

            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "task_complete":
                    commit_msg = f"{commit_prefix} {block.input['commit_message']}"
                    paths = block.input["paths_changed"]

                    # Commit the work via gateway.
                    _call_gateway(
                        http,
                        gateway_url,
                        "/git/commit",
                        {
                            "agent_id": agent_id,
                            "task_id": task_id,
                            "repo_path": repo_path,
                            "message": commit_msg,
                            "paths": paths,
                        },
                    )

                    # Transition task to completed via orchestrator.
                    http.post(
                        f"{orchestrator_url}/tasks/{task_id}/transition",
                        json={"new_status": "completed", "actor": agent_id},
                    ).raise_for_status()

                    completed = True
                    break

                # Execute regular gateway tool.
                try:
                    result_text = _execute_gateway_tool(
                        block.name,
                        block.input,
                        agent_id,
                        task_id,
                        repo_path,
                        gateway_url,
                        http,
                    )
                except httpx.HTTPStatusError as exc:
                    result_text = f"Gateway error {exc.response.status_code}: {exc.response.text}"

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

            if completed:
                _finish("success")
                return "completed"

            # Feed tool results back and continue.
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        # Exhausted max_iterations.
        http.post(
            f"{orchestrator_url}/tasks/{task_id}/transition",
            json={
                "new_status": "failed",
                "actor": agent_id,
                "details": {"reason": "max_iterations exceeded"},
            },
        )
        return _finish("failed")
