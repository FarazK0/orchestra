"""Lightweight tests for the QA agent package.

No LLM calls, no gateway, no DB -- purely structural checks.
"""

from __future__ import annotations

import json
from pathlib import Path

_AGENT_DIR = Path(__file__).parent.parent / "qa"


def test_agent_json_has_correct_id():
    data = json.loads((_AGENT_DIR / "agent.json").read_text())
    assert data["id"] == "qa-agent"
    assert data["schema_version"] == 1
    assert "skills" in data


def test_prompt_md_exists_and_nonempty():
    prompt = (_AGENT_DIR / "prompt.md").read_text(encoding="utf-8")
    assert len(prompt.strip()) > 0


def test_prompt_md_contains_qa_guidance():
    prompt = (_AGENT_DIR / "prompt.md").read_text(encoding="utf-8")
    assert "QA Report" in prompt


def test_main_module_is_importable():
    import agents.qa.main  # noqa: F401
