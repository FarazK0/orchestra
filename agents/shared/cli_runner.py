"""Shared backend registry and CLI runner for Orchestra agent components.

Supports configuring the worker, planner, and validator independently through
permissions/backends.yaml, env vars (WORKER_BACKEND, PLANNER_BACKEND,
VALIDATOR_BACKEND), or ~/.config/orchestra/config.

Priority for each component:
  env var → config file → backends.yaml defaults → "claude-code"
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

_BACKENDS_PATH = Path(__file__).parent.parent.parent / "permissions" / "backends.yaml"

_ENV_VAR: dict[str, str] = {
    "worker": "WORKER_BACKEND",
    "planner": "PLANNER_BACKEND",
    "validator": "VALIDATOR_BACKEND",
}

_CFG_KEY: dict[str, str] = {
    "worker": "worker_backend",
    "planner": "planner_backend",
    "validator": "validator_backend",
}


def load_backends(registry_path: Path | None = None) -> dict[str, Any]:
    """Load permissions/backends.yaml. Returns {name: config_dict}."""
    path = registry_path or _BACKENDS_PATH
    if not path.exists():
        return {
            "claude-code": {
                "type": "cli",
                "command": ["claude", "--dangerously-skip-permissions", "-p", "-"],
                "input": "stdin",
            },
            "python-api": {"type": "python"},
        }
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("backends", {})


def _load_defaults(registry_path: Path | None = None) -> dict[str, str]:
    """Load the defaults section from backends.yaml."""
    path = registry_path or _BACKENDS_PATH
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("defaults", {})


def get_backend(component: str, registry_path: Path | None = None) -> str:
    """Resolve backend name for 'worker' | 'planner' | 'validator'.

    Priority: env var > ~/.config/orchestra/config > backends.yaml defaults > 'claude-code'
    Also accepts legacy AGENT_TYPE env var as a fallback for 'worker' only:
      AGENT_TYPE=claude-code → claude-code
      AGENT_TYPE=python      → python-api
    """
    env_key = _ENV_VAR.get(component, f"{component.upper()}_BACKEND")
    val = os.getenv(env_key, "").strip()
    if val:
        return val

    cfg_key = _CFG_KEY.get(component, f"{component}_backend")
    cfg_path = Path.home() / ".config" / "orchestra" / "config"
    if cfg_path.exists():
        for line in cfg_path.read_text().splitlines():
            if line.startswith(f"{cfg_key}="):
                v = line.split("=", 1)[1].strip()
                if v:
                    return v

    yaml_default = _load_defaults(registry_path).get(component, "")
    if yaml_default:
        return yaml_default

    # Legacy AGENT_TYPE support (worker only).
    if component == "worker":
        agent_type = os.getenv("AGENT_TYPE", "").strip()
        if agent_type == "python":
            return "python-api"
        if agent_type and agent_type != "claude-code":
            return agent_type

    return "claude-code"


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def run_cli_prompt(
    backend_name: str,
    prompt: str,
    timeout: int = 300,
    registry_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Invoke a CLI backend with a prompt string, return stdout.

    Raises RuntimeError on non-zero exit or timeout.
    Strips ANSI escape codes from output.
    """
    backends = load_backends(registry_path)
    cfg = backends.get(backend_name)
    if cfg is None:
        raise RuntimeError(
            f"Backend '{backend_name}' not found in permissions/backends.yaml. "
            f"Available: {', '.join(backends) or '(none)'}. "
            "Run 'orchctl backend list' to see available backends."
        )
    if cfg.get("type") == "python":
        raise ValueError(
            f"Backend '{backend_name}' is a python-api backend — use LLMClient directly, "
            "not run_cli_prompt()."
        )

    command: list[str] = cfg.get("command", [])
    input_mode: str = cfg.get("input", "stdin")

    if not command:
        raise RuntimeError(f"Backend '{backend_name}' has no command configured.")

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    if extra_env:
        env.update(extra_env)

    tmp_path: str | None = None
    try:
        if input_mode == "stdin":
            result = subprocess.run(
                command,
                input=prompt,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        elif input_mode == "arg":
            result = subprocess.run(
                command + [prompt],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        elif input_mode == "file":
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(prompt)
                tmp_path = f.name
            result = subprocess.run(
                command + [tmp_path],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            raise RuntimeError(f"Unknown input mode '{input_mode}' for backend '{backend_name}'.")
    except FileNotFoundError:
        exe = command[0] if command else backend_name
        raise RuntimeError(
            f"Worker backend '{backend_name}' is not available.\n"
            f"Command '{exe}' not found. To fix:\n"
            f"  - Install {exe}, or\n"
            f"  - Switch backend: orchctl config set worker-backend claude-code"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Backend '{backend_name}' timed out after {timeout}s.")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"Backend '{backend_name}' exited {result.returncode}: {stderr[:500]}")

    return _strip_ansi(result.stdout)
