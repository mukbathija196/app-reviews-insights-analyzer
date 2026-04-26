"""Token budget governor for free-tier LLM providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


class BudgetExceeded(Exception):
    """Raised when a run would exceed the configured token or request budget."""


Provider = Literal["gemini", "groq", "ollama", "unknown"]


@dataclass(frozen=True)
class BudgetSnapshot:
    requests: int
    tokens_in: int
    tokens_out: int


class TokenBudget:
    """Tracks token and request usage; raises BudgetExceeded before overshoot."""

    def __init__(
        self,
        max_tokens_in: int = 60_000,
        max_tokens_out: int = 4_000,
        max_requests: int = 10,
        *,
        provider: Provider = "unknown",
    ) -> None:
        self.max_tokens_in = max_tokens_in
        self.max_tokens_out = max_tokens_out
        self.max_requests = max_requests
        self.provider = provider
        self._tokens_in = 0
        self._tokens_out = 0
        self._requests = 0

    def check_and_reserve(self, tokens_in: int) -> None:
        """Assert the next request stays within budget."""
        if tokens_in < 0:
            raise ValueError("tokens_in must be >= 0")
        next_requests = self._requests + 1
        next_in = self._tokens_in + tokens_in
        if next_requests > self.max_requests:
            raise BudgetExceeded(
                f"Request budget exceeded: {next_requests} > {self.max_requests}"
            )
        if next_in > self.max_tokens_in:
            raise BudgetExceeded(
                f"Input token budget exceeded: {next_in} > {self.max_tokens_in}"
            )
        self._requests = next_requests
        self._tokens_in = next_in

    def reserve_from_text(self, text: str) -> int:
        """Count and reserve input tokens from plain text."""
        tokens = count_tokens(text, provider=self.provider)
        self.check_and_reserve(tokens)
        return tokens

    def record_output(self, tokens_out: int) -> None:
        """Record output tokens and enforce output budget."""
        if tokens_out < 0:
            raise ValueError("tokens_out must be >= 0")
        next_out = self._tokens_out + tokens_out
        if next_out > self.max_tokens_out:
            raise BudgetExceeded(
                f"Output token budget exceeded: {next_out} > {self.max_tokens_out}"
            )
        self._tokens_out = next_out

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            requests=self._requests,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


__all__ = ["TokenBudget", "BudgetExceeded"]


def count_tokens(text: str, provider: Provider = "unknown") -> int:
    """Best-effort provider-specific token estimate with graceful fallback."""
    stripped = text.strip()
    if not stripped:
        return 0

    # Groq commonly serves Llama-family models; cl100k_base is a safe
    # approximation when exact tokenizer is unavailable.
    if provider in {"groq", "gemini", "ollama", "unknown"}:
        try:
            import tiktoken

            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(stripped))
        except Exception:
            pass

    # For local models or fallback: conservative heuristic.
    word_count = len(re.findall(r"\S+", stripped))
    char_count = len(stripped)
    return max(1, int(word_count * 1.4), int(char_count / 4))
