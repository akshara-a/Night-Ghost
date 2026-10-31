from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class LLMMessage(BaseModel):
    role: str  # system, user, assistant, tool
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]


class ToolCall(BaseModel):
    id: str = "call_default"
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: List[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    raw_response: Dict[str, Any] = Field(default_factory=dict)


class BaseLLMClient(abc.ABC):
    """Abstract base class for local LLM clients."""

    @abc.abstractmethod
    def generate(
        self,
        messages: List[LLMMessage],
        tools: Optional[List[ToolDefinition]] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Generate response from model given chat history and optional tools."""
        pass

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Verify model connectivity and readiness."""
        pass
