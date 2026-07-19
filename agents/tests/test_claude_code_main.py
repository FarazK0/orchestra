"""Unit tests for the claude-code-agent main module.

These tests do not require the claude CLI, Postgres, or a running gateway.
subprocess and httpx are mocked so the tests run offline.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(tmp_path, task_id: str = "TASK-001") -> str:
    """Write a minimal context package JSON and return its path as a string."""
    pkg = {
        "task_id": task_id,
        "task": {
            "id": task_id,
            "owner": "backend-agent",
            "title": "Test task",
            "inputs": [],
            "outputs": [],
            "acceptance": [],
            "risk_tier": 1,
            "budget": {"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        },
        "agent_instructions": {
            "branch": f"agent/backend/{task_id}",
            "commit_prefix": f"[{task_id}]",
            "read_scope": [],
            "write_scope": [],
            "acceptance_criteria": [],
        },
        "capability_token": "",
        "input_artifacts": [],
        "agent_memory": None,
    }
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps(pkg))
    return str(ctx)


def _mock_http_client(call_log: list):
    """Return a mock httpx.Client whose request() and get() methods record calls and return canned JSON."""

    def _request(method, url, **kwargs):
        body = kwargs.get("json", {})
        call_log.append({"method": method, "url": url, "json": body})
        if "/git/branch" in url:
            resp_body = {"branch": "agent/backend/TASK-001", "created": True}
        elif "/git/commit" in url:
            resp_body = {"sha": "abc1234"}
        elif "/emit_event" in url:
            resp_body = {"event_id": "evt-001"}
        elif "/transition" in url:
            resp_body = {"id": "TASK-001", "status": "completed"}
        elif "/memory/upsert" in url:
            resp_body = {}
        else:
            resp_body = {}
        mock_resp = MagicMock()
        mock_resp.json.return_value = resp_body
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def _get(url, **kwargs):
        call_log.append({"method": "GET", "url": url, "params": kwargs.get("params", {})})
        # Default: no TASK_DISCOVERED events (normal completion flow)
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.side_effect = _request
    mock_client.get.side_effect = _get
    return mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_file_write_audit_emitted_after_commit(tmp_path):
    """After a successful commit, claude-code-agent emits CLAUDE_CODE_FILES_WRITTEN."""
    call_log: list = []
    ctx = _make_context(tmp_path)

    def _check_output_side_effect(cmd, **kwargs):
        if "diff" in cmd:
            return "src/app.py\ntests/test_app.py\n"
        return ""  # ls-files: no untracked files

    with (
        patch("subprocess.run") as mock_run,
        patch("subprocess.check_output", side_effect=_check_output_side_effect),
        patch("httpx.Client", return_value=_mock_http_client(call_log)),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")

        from typer.testing import CliRunner

        from agents.claude_code.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["--context", ctx, "--run-id", "run-abc"])

    assert result.exit_code == 0, result.output

    emit_calls = [c for c in call_log if "/emit_event" in c["url"]]
    assert len(emit_calls) == 1

    payload = emit_calls[0]["json"]
    assert payload["event_type"] == "CLAUDE_CODE_FILES_WRITTEN"
    assert payload["agent_id"] == "backend-agent"
    assert payload["task_id"] == "TASK-001"
    assert "src/app.py" in payload["payload"]["paths"]
    assert "tests/test_app.py" in payload["payload"]["paths"]
    assert payload["payload"]["sha"] == "abc1234"
    assert payload["payload"]["run_id"] == "run-abc"


def test_emit_event_called_before_transition(tmp_path):
    """emit_event (audit) is called before the task-completed transition."""
    call_log: list = []
    ctx = _make_context(tmp_path)

    with (
        patch("subprocess.run") as mock_run,
        patch("subprocess.check_output", return_value=" M output.py\n"),
        patch("httpx.Client", return_value=_mock_http_client(call_log)),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        from typer.testing import CliRunner

        from agents.claude_code.main import app

        runner = CliRunner()
        runner.invoke(app, ["--context", ctx, "--run-id", "run-xyz"])

    urls = [c["url"] for c in call_log]
    emit_idx = next((i for i, u in enumerate(urls) if "/emit_event" in u), None)
    transition_idx = next((i for i, u in enumerate(urls) if "/transition" in u), None)

    assert emit_idx is not None, "emit_event was not called"
    assert transition_idx is not None, "transition was not called"
    assert emit_idx < transition_idx, "emit_event should be called before transition"


def test_task_discovered_exits_cleanly_without_completion(tmp_path):
    """When TASK_DISCOVERED is emitted, main exits 0 without transitioning to completed."""
    call_log: list = []
    ctx = _make_context(tmp_path)

    def _post_request(method, url, **kwargs):
        body = kwargs.get("json", {})
        call_log.append({"method": method, "url": url, "json": body})
        if "/git/branch" in url:
            resp_body = {"branch": "agent/backend/TASK-001", "created": True}
        elif "/git/commit" in url:
            resp_body = {"sha": "abc1234"}
        elif "/emit_event" in url:
            resp_body = {"event_id": "evt-discovery"}
        elif "/transition" in url:
            resp_body = {"id": "TASK-001", "status": "completed"}
        else:
            resp_body = {}
        mock_resp = MagicMock()
        mock_resp.json.return_value = resp_body
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def _get_request(url, **kwargs):
        call_log.append({"method": "GET", "url": url, "params": kwargs.get("params", {})})
        # Return a TASK_DISCOVERED event for the events endpoint
        if "/events" in url:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"event_type": "TASK_DISCOVERED"}]
            mock_resp.raise_for_status.return_value = None
            return mock_resp
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.side_effect = _post_request
    mock_client.get.side_effect = _get_request

    # Git status shows one file (partial work before discovery)
    with (
        patch("subprocess.run") as mock_run,
        patch("subprocess.check_output", return_value=" M partial.py\n"),
        patch("httpx.Client", return_value=mock_client),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")

        from typer.testing import CliRunner

        from agents.claude_code.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["--context", ctx, "--run-id", "run-disc"])

    # Should exit 0 (clean suspension)
    assert result.exit_code == 0, result.output

    # Must NOT have transitioned to completed
    transition_calls = [c for c in call_log if "/transition" in c.get("url", "")]
    assert transition_calls == [], f"Should not transition to completed; got: {transition_calls}"

    # Should have committed the partial work
    commit_calls = [c for c in call_log if "/git/commit" in c.get("url", "")]
    assert len(commit_calls) == 1
    assert commit_calls[0]["json"]["message"].endswith("partial work before discovery")


def test_audit_emit_failure_is_non_fatal(tmp_path):
    """A failing emit_event call does not prevent task completion."""
    call_log: list = []
    ctx = _make_context(tmp_path)

    def _request_with_emit_failure(method, url, **kwargs):
        body = kwargs.get("json", {})
        call_log.append({"method": method, "url": url, "json": body})
        if "/emit_event" in url:
            raise Exception("network error")
        if "/git/branch" in url:
            resp_body = {"branch": "agent/backend/TASK-001", "created": True}
        elif "/git/commit" in url:
            resp_body = {"sha": "def5678"}
        elif "/transition" in url:
            resp_body = {"id": "TASK-001", "status": "completed"}
        elif "/memory/upsert" in url:
            resp_body = {}
        else:
            resp_body = {}
        mock_resp = MagicMock()
        mock_resp.json.return_value = resp_body
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def _get_no_discovery(url, **kwargs):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.side_effect = _request_with_emit_failure
    mock_client.get.side_effect = _get_no_discovery

    with (
        patch("subprocess.run") as mock_run,
        patch("subprocess.check_output", return_value=" M output.py\n"),
        patch("httpx.Client", return_value=mock_client),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        from typer.testing import CliRunner

        from agents.claude_code.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["--context", ctx, "--run-id", "run-fail"])

    # Task should still complete even though emit_event failed
    assert result.exit_code == 0, result.output
    transition_calls = [c for c in call_log if "/transition" in c["url"]]
    assert len(transition_calls) == 1
    assert transition_calls[0]["json"]["new_status"] == "completed"
