"""Tests for agents.shared.cli_runner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_backends_yaml(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "backends.yaml"
    p.write_text(yaml.dump(content))
    return p


# ---------------------------------------------------------------------------
# load_backends
# ---------------------------------------------------------------------------


def test_load_backends_returns_dict(tmp_path):
    registry = _write_backends_yaml(
        tmp_path,
        {
            "backends": {
                "my-agent": {"type": "cli", "command": ["my-cli", "-p", "-"], "input": "stdin"}
            },
            "defaults": {"worker": "my-agent"},
        },
    )
    from agents.shared.cli_runner import load_backends

    result = load_backends(registry)
    assert "my-agent" in result
    assert result["my-agent"]["command"] == ["my-cli", "-p", "-"]


def test_load_backends_returns_fallback_when_missing():
    from agents.shared.cli_runner import load_backends

    result = load_backends(Path("/nonexistent/path/backends.yaml"))
    assert "claude-code" in result
    assert "python-api" in result


# ---------------------------------------------------------------------------
# get_backend
# ---------------------------------------------------------------------------


def test_get_backend_env_var_takes_priority(tmp_path, monkeypatch):
    registry = _write_backends_yaml(
        tmp_path,
        {
            "backends": {"gemini": {"type": "cli", "command": ["gemini"]}},
            "defaults": {"worker": "claude-code"},
        },
    )
    monkeypatch.setenv("WORKER_BACKEND", "gemini")
    from importlib import reload

    import agents.shared.cli_runner as cr

    reload(cr)
    result = cr.get_backend("worker", registry)
    assert result == "gemini"
    monkeypatch.delenv("WORKER_BACKEND", raising=False)


def test_get_backend_yaml_default(tmp_path, monkeypatch):
    registry = _write_backends_yaml(
        tmp_path,
        {"backends": {"my-tool": {}}, "defaults": {"worker": "my-tool"}},
    )
    monkeypatch.delenv("WORKER_BACKEND", raising=False)
    from agents.shared.cli_runner import get_backend

    result = get_backend("worker", registry)
    assert result == "my-tool"


def test_get_backend_fallback_default(monkeypatch):
    monkeypatch.delenv("WORKER_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_TYPE", raising=False)
    from agents.shared.cli_runner import get_backend

    result = get_backend("worker", Path("/nonexistent/backends.yaml"))
    assert result == "claude-code"


def test_get_backend_agent_type_python_maps_to_python_api(monkeypatch):
    monkeypatch.delenv("WORKER_BACKEND", raising=False)
    monkeypatch.setenv("AGENT_TYPE", "python")
    from agents.shared.cli_runner import get_backend

    result = get_backend("worker", Path("/nonexistent/backends.yaml"))
    assert result == "python-api"
    monkeypatch.delenv("AGENT_TYPE", raising=False)


# ---------------------------------------------------------------------------
# run_cli_prompt
# ---------------------------------------------------------------------------


def test_run_cli_prompt_stdin_mode(tmp_path):
    registry = _write_backends_yaml(
        tmp_path,
        {"backends": {"test-cli": {"type": "cli", "command": ["echo"], "input": "stdin"}}},
    )
    mock_result = MagicMock(returncode=0, stdout="hello\n", stderr="")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        from agents.shared.cli_runner import run_cli_prompt

        out = run_cli_prompt("test-cli", "my prompt", registry_path=registry)
    assert out == "hello\n"
    call_kwargs = mock_run.call_args
    assert call_kwargs[1]["input"] == "my prompt"


def test_run_cli_prompt_raises_on_nonzero(tmp_path):
    registry = _write_backends_yaml(
        tmp_path,
        {"backends": {"fail-cli": {"type": "cli", "command": ["false"], "input": "stdin"}}},
    )
    mock_result = MagicMock(returncode=1, stdout="", stderr="something failed")
    with patch("subprocess.run", return_value=mock_result):
        from agents.shared.cli_runner import run_cli_prompt

        with pytest.raises(RuntimeError, match="exited 1"):
            run_cli_prompt("fail-cli", "prompt", registry_path=registry)


def test_run_cli_prompt_raises_for_missing_backend(tmp_path):
    registry = _write_backends_yaml(tmp_path, {"backends": {}})
    from agents.shared.cli_runner import run_cli_prompt

    with pytest.raises(RuntimeError, match="not found in permissions/backends.yaml"):
        run_cli_prompt("nonexistent", "prompt", registry_path=registry)


def test_run_cli_prompt_raises_for_python_backend(tmp_path):
    registry = _write_backends_yaml(tmp_path, {"backends": {"python-api": {"type": "python"}}})
    from agents.shared.cli_runner import run_cli_prompt

    with pytest.raises(ValueError, match="python-api backend"):
        run_cli_prompt("python-api", "prompt", registry_path=registry)


def test_run_cli_prompt_strips_ansi(tmp_path):
    registry = _write_backends_yaml(
        tmp_path,
        {"backends": {"color-cli": {"type": "cli", "command": ["echo"], "input": "stdin"}}},
    )
    ansi_output = "\x1b[32mgreen text\x1b[0m\n"
    mock_result = MagicMock(returncode=0, stdout=ansi_output, stderr="")
    with patch("subprocess.run", return_value=mock_result):
        from agents.shared.cli_runner import run_cli_prompt

        out = run_cli_prompt("color-cli", "prompt", registry_path=registry)
    assert out == "green text\n"
