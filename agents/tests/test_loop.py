"""Unit tests for the agent loop and LLM client.

These tests do not require Postgres, a running gateway, or an Anthropic API key.
The Anthropic SDK is mocked via unittest.mock; gateway HTTP calls are intercepted
with httpx.MockTransport.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from agents.shared.loop import GATEWAY_TOOLS, format_context_package, run_agent_loop


# ---------------------------------------------------------------------------
# Helpers: build minimal fake objects that the code touches
# ---------------------------------------------------------------------------


def _make_pkg(
    task_id: str = "TASK-001",
    title: str = "Add health endpoint",
    acceptance: list[str] | None = None,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    input_artifacts: list[dict] | None = None,
    adrs: list[dict] | None = None,
) -> dict:
    if acceptance is None:
        acceptance = ["GET /health returns 200", "pytest passes"]
    if inputs is None:
        inputs = ["app/main.py"]
    if outputs is None:
        outputs = ["app/main.py"]
    if input_artifacts is None:
        input_artifacts = [{"path": "app/main.py", "content": "# existing", "found": True}]
    return {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": str(uuid.uuid4()),
        "packaged_at": datetime.now(timezone.utc).isoformat(),
        "task": {
            "id": task_id,
            "title": title,
            "owner": "backend-agent",
            "status": "running",
            "depends_on": [],
            "inputs": inputs,
            "outputs": outputs,
            "acceptance": acceptance,
            "risk_tier": 1,
            "budget": {"tokens": 100_000, "wall_clock_min": 30, "retries": 2},
        },
        "input_artifacts": input_artifacts,
        "adrs": adrs or [],
        "agent_instructions": {
            "agent_id": "backend-agent",
            "branch": f"agent/backend/{task_id}",
            "commit_prefix": f"[{task_id}]",
            "read_scope": inputs,
            "write_scope": outputs,
            "acceptance_criteria": acceptance,
        },
    }


def _make_tool_use_block(name: str, tool_input: dict, tool_id: str | None = None):
    block = SimpleNamespace()
    block.type = "tool_use"
    block.name = name
    block.input = tool_input
    block.id = tool_id or f"tu_{name}"
    return block


def _make_text_block(text: str):
    block = SimpleNamespace()
    block.type = "text"
    block.text = text
    return block


def _make_llm_response(
    content: list,
    stop_reason: str = "tool_use",
    input_tokens: int = 10,
    output_tokens: int = 5,
):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


# ---------------------------------------------------------------------------
# format_context_package
# ---------------------------------------------------------------------------


class TestFormatContextPackage:
    def test_includes_title(self):
        pkg = _make_pkg(title="Add /health endpoint")
        out = format_context_package(pkg)
        assert "Add /health endpoint" in out

    def test_includes_acceptance_criteria(self):
        pkg = _make_pkg(acceptance=["GET /health returns 200", "pytest passes"])
        out = format_context_package(pkg)
        assert "GET /health returns 200" in out
        assert "pytest passes" in out

    def test_wraps_file_content_in_fences(self):
        pkg = _make_pkg(
            input_artifacts=[{"path": "app/main.py", "content": "x = 1", "found": True}]
        )
        out = format_context_package(pkg)
        assert "```" in out
        assert "x = 1" in out

    def test_notes_missing_file(self):
        pkg = _make_pkg(input_artifacts=[{"path": "new.py", "content": None, "found": False}])
        out = format_context_package(pkg)
        assert "new.py" in out
        assert "does not exist" in out

    def test_adrs_labelled_read_only(self):
        pkg = _make_pkg(adrs=[{"path": "docs/adr/ADR-001.md", "content": "# foo"}])
        out = format_context_package(pkg)
        assert "read-only" in out or "not instructions" in out

    def test_includes_branch(self):
        pkg = _make_pkg(task_id="TASK-042")
        out = format_context_package(pkg)
        assert "agent/backend/TASK-042" in out

    def test_includes_task_complete_instruction(self):
        pkg = _make_pkg()
        out = format_context_package(pkg)
        assert "task_complete" in out


# ---------------------------------------------------------------------------
# GATEWAY_TOOLS shape
# ---------------------------------------------------------------------------


class TestGatewayTools:
    def test_all_expected_tools_present(self):
        names = {t["name"] for t in GATEWAY_TOOLS}
        assert "read_artifact" in names
        assert "write_artifact" in names
        assert "run_command" in names
        assert "emit_event" in names
        assert "task_complete" in names

    def test_tools_have_required_fields(self):
        for tool in GATEWAY_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool


# ---------------------------------------------------------------------------
# LLMClient token recording
# ---------------------------------------------------------------------------


class TestLLMClientTokenRecording:
    def test_records_tokens_to_run(self):
        """LLMClient.call increments run.tokens_used and run.cost_usd."""
        from agents.shared.llm import LLMClient

        fake_run = SimpleNamespace(
            run_id=uuid.uuid4(),
            tokens_used=0,
            cost_usd=0.0,
        )
        session = MagicMock()
        session.get.return_value = fake_run

        fake_response = _make_llm_response(
            content=[_make_text_block("hi")],
            stop_reason="end_turn",
            input_tokens=100,
            output_tokens=50,
        )

        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = fake_response
            client = LLMClient(api_key="test-key", model="claude-opus-4-8")
            client.call(
                messages=[{"role": "user", "content": "hello"}],
                system="sys",
                run_id=fake_run.run_id,
                session=session,
            )

        assert fake_run.tokens_used == 150  # 100 in + 50 out
        # cost = (100 * 5.00 + 50 * 25.00) / 1_000_000 = 0.001750
        assert abs(fake_run.cost_usd - 0.001750) < 1e-9

    def test_no_crash_when_run_missing(self):
        """Token recording is best-effort; missing run does not raise."""
        from agents.shared.llm import LLMClient

        session = MagicMock()
        session.get.return_value = None  # run not found

        fake_response = _make_llm_response(
            content=[_make_text_block("ok")],
            stop_reason="end_turn",
            input_tokens=5,
            output_tokens=5,
        )

        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = fake_response
            client = LLMClient(api_key="test-key", model="claude-opus-4-8")
            # Should not raise
            client.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                run_id=uuid.uuid4(),
                session=session,
            )


# ---------------------------------------------------------------------------
# run_agent_loop
# ---------------------------------------------------------------------------


def _make_httpx_transport(responses: list[dict]) -> httpx.MockTransport:
    """Return a MockTransport that serves the given JSON responses in order."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(call_count[0], len(responses) - 1)
        call_count[0] += 1
        payload = responses[idx]
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


class TestRunAgentLoop:
    def _make_run_row(self, run_id: uuid.UUID | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            run_id=run_id or uuid.uuid4(),
            tokens_used=0,
            cost_usd=0.0,
            finished_at=None,
            result=None,
        )

    def test_completes_on_task_complete_tool(self):
        """Loop returns 'completed' when agent calls task_complete on the first turn."""
        pkg = _make_pkg()
        run_id = uuid.uuid4()
        fake_run = self._make_run_row(run_id)
        session = MagicMock()
        session.get.return_value = fake_run

        task_complete_block = _make_tool_use_block(
            "task_complete",
            {"commit_message": "add health endpoint", "paths_changed": ["app/main.py"]},
            tool_id="tc_1",
        )
        first_response = _make_llm_response([task_complete_block], stop_reason="tool_use")

        llm = MagicMock()
        llm.call.return_value = first_response

        # Gateway: branch + commit; orchestrator: transition
        gateway_responses = [
            {"branch": "agent/backend/TASK-001", "created": True},  # /git/branch
            {"sha": "abc123"},  # /git/commit
        ]
        orch_responses = [
            {"id": "TASK-001", "status": "completed"},  # /tasks/.../transition
        ]

        call_idx = [0]
        all_responses = gateway_responses + orch_responses

        def _handler(request: httpx.Request) -> httpx.Response:
            idx = call_idx[0]
            call_idx[0] += 1
            return httpx.Response(200, json=all_responses[min(idx, len(all_responses) - 1)])

        with patch("httpx.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
            MockClient.return_value = mock_client

            result = run_agent_loop(
                context_package=pkg,
                repo_path="/tmp/repo",
                gateway_url="http://gw",
                orchestrator_url="http://orch",
                llm=llm,
                system_prompt="sys",
                run_id=run_id,
                session=session,
            )

        assert result == "completed"
        assert fake_run.result == "success"
        assert fake_run.finished_at is not None

    def test_fails_after_max_iterations(self):
        """Loop returns 'failed' when max_iterations is exhausted."""
        pkg = _make_pkg()
        run_id = uuid.uuid4()
        fake_run = self._make_run_row(run_id)
        session = MagicMock()
        session.get.return_value = fake_run

        # Always return a write_artifact tool call (never task_complete)
        write_block = _make_tool_use_block(
            "write_artifact",
            {"path": "app/main.py", "content": "x = 1"},
            tool_id="w_1",
        )
        llm = MagicMock()
        llm.call.return_value = _make_llm_response([write_block], stop_reason="tool_use")

        with patch("httpx.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            post_mock = MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
            )
            post_mock.json.return_value = {"written": True}
            mock_client.post.return_value = post_mock
            MockClient.return_value = mock_client

            result = run_agent_loop(
                context_package=pkg,
                repo_path="/tmp/repo",
                gateway_url="http://gw",
                orchestrator_url="http://orch",
                llm=llm,
                system_prompt="sys",
                run_id=run_id,
                session=session,
                max_iterations=2,
            )

        assert result == "failed"
        assert fake_run.result == "failed"
        assert fake_run.finished_at is not None
        assert llm.call.call_count == 2

    def test_fails_on_end_turn_without_task_complete(self):
        """Loop returns 'failed' if Claude stops with end_turn (no task_complete)."""
        pkg = _make_pkg()
        run_id = uuid.uuid4()
        fake_run = self._make_run_row(run_id)
        session = MagicMock()
        session.get.return_value = fake_run

        llm = MagicMock()
        llm.call.return_value = _make_llm_response(
            [_make_text_block("I am done")],
            stop_reason="end_turn",
        )

        with patch("httpx.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"branch": "x", "created": True}),
            )
            MockClient.return_value = mock_client

            result = run_agent_loop(
                context_package=pkg,
                repo_path="/tmp/repo",
                gateway_url="http://gw",
                orchestrator_url="http://orch",
                llm=llm,
                system_prompt="sys",
                run_id=run_id,
                session=session,
            )

        assert result == "failed"
        assert fake_run.result == "failed"
