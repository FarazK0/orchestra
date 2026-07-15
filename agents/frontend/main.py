"""Frontend agent entry point.

Reads a context package JSON, runs the agent loop, and exits with code 0
on success or 1 on failure.

Usage:
    python -m agents.frontend.main \\
        --context /path/to/<run_id>.json \\
        --run-id <uuid> \\
        [--repo PATH] [--gateway-url URL] [--orchestrator-url URL]
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(name="frontend-agent", add_completion=False)

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


@app.command()
def main(
    context: str = typer.Option(..., "--context", "-c", help="Path to context package JSON."),
    run_id: str = typer.Option(..., "--run-id", help="Run UUID (from runs.run_id)."),
    repo: str = typer.Option(
        None,
        "--repo",
        "-r",
        help="Managed repo path. Defaults to $SANDBOX_REPO_PATH.",
    ),
    gateway_url: str = typer.Option(
        None,
        "--gateway-url",
        help="Gateway base URL. Defaults to $GATEWAY_URL or http://localhost:8081.",
    ),
    orchestrator_url: str = typer.Option(
        None,
        "--orchestrator-url",
        help="Orchestrator base URL. Defaults to $ORCHESTRATOR_URL or http://localhost:8080.",
    ),
) -> None:
    """Run the frontend agent loop for a given context package."""
    from agents.shared.llm import LLMClient
    from agents.shared.loop import run_agent_loop
    from orchestrator.orchestrator.db import get_engine, get_session_factory

    pkg = json.loads(Path(context).read_text(encoding="utf-8"))
    repo_path = repo or os.getenv("SANDBOX_REPO_PATH", "./sandbox/sample-project")
    gw_url = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8081")
    orch_url = orchestrator_url or os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    engine = get_engine()
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        session.begin()
        try:
            llm = LLMClient()
            result = run_agent_loop(
                context_package=pkg,
                repo_path=repo_path,
                gateway_url=gw_url,
                orchestrator_url=orch_url,
                llm=llm,
                system_prompt=system_prompt,
                run_id=uuid.UUID(run_id),
                session=session,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise

    typer.echo(f"Agent loop result: {result}")
    if result != "completed":
        sys.exit(1)


if __name__ == "__main__":
    app()
