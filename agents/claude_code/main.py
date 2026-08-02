"""Backward-compatibility shim — use agents.worker.main instead.

This module is kept so that any external scripts or run records that reference
'agents.claude_code.main' continue to work. All logic has moved to agents.worker.main.
"""

from agents.worker.main import app, main  # noqa: F401

if __name__ == "__main__":
    app()
