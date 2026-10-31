from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from nightghost.llm.base import BaseLLMClient, LLMMessage, LLMResponse, ToolCall, ToolDefinition


class MockLLMClient(BaseLLMClient):
    """Deterministic mock LLM client for tests, dry-runs, and scripted evaluations."""

    def __init__(
        self,
        canned_responses: Optional[List[LLMResponse]] = None,
        generator_fn: Optional[Callable[[List[LLMMessage], Optional[List[ToolDefinition]]], LLMResponse]] = None,
    ):
        self.canned_responses = list(canned_responses) if canned_responses else []
        self.generator_fn = generator_fn
        self.call_history: List[List[LLMMessage]] = []

    def is_available(self) -> bool:
        return True

    def generate(
        self,
        messages: List[LLMMessage],
        tools: Optional[List[ToolDefinition]] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.call_history.append(messages)

        if self.generator_fn:
            return self.generator_fn(messages, tools)

        if self.canned_responses:
            return self.canned_responses.pop(0)

        # Default fallback response
        return LLMResponse(
            content="Mock LLM response: Task verified.",
            finish_reason="stop",
        )
