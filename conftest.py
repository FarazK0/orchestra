"""Root conftest: ensure the repo root is on sys.path for all test modules.

Without this, `uv run pytest` uses an isolated venv whose sys.path does not
include the project root, so `import orchestrator.orchestrator.*` fails even
though pyproject.toml declares pythonpath = ["."].  A root conftest is the
most reliable injection point because pytest loads it before collecting tests.
"""

import sys
from pathlib import Path

_root = str(Path(__file__).parent)
if _root not in sys.path:
    sys.path.insert(0, _root)
