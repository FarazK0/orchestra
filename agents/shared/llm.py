"""LLM client wrapper — the single path to Claude API calls.

All provider calls go through here. After each call the wrapper records
input tokens, output tokens, and cost into the Run row so the control
plane has accurate per-run cost accounting.

Usage:
    client = LLMClient()
    response = client.call(
        messages=[{"role": "user", "content": "hello"}],
        system="You are...",
        run_id=run_id,
        session=session,
    )
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import anthropic
from sqlalchemy.orm import Session

from orchestrator.orchestrator.db import Run

# Pricing in USD per 1M tokens: (input_price, output_price)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = _PRICING.get(model, (5.00, 25.00))
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


class LLMClient:
    """Thin wrapper around the Anthropic SDK that records usage to the DB."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model: str = model or os.getenv("LLM_MODEL", "claude-opus-4-8")

    def call(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        run_id: uuid.UUID | None = None,
        session: Session | None = None,
        max_tokens: int = 4096,
    ) -> anthropic.types.Message:
        """Call Claude and record token usage.

        Args:
            messages:   Conversation history in Anthropic message format.
            system:     System prompt string.
            tools:      Optional tool definitions (Anthropic JSON schema format).
            run_id:     If given, the Run row's tokens_used/cost_usd are updated.
            session:    Open SQLAlchemy Session for the Run update. Required when
                        run_id is given.
            max_tokens: Maximum output tokens.

        Returns:
            The raw anthropic.types.Message.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = self._client.messages.create(**kwargs)

        # Record cost into the Run row (best-effort; never raise on accounting errors).
        if run_id is not None and session is not None:
            try:
                run = session.get(Run, run_id)
                if run is not None:
                    in_tok = response.usage.input_tokens
                    out_tok = response.usage.output_tokens
                    run.tokens_used = (run.tokens_used or 0) + in_tok + out_tok
                    cost = _cost(self.model, in_tok, out_tok)
                    run.cost_usd = float(run.cost_usd or 0) + cost
                    session.flush()
            except Exception:
                pass

        return response
