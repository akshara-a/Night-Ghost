from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
import httpx

from nightghost.config import ModelConfig
from nightghost.llm.base import BaseLLMClient, LLMMessage, LLMResponse, ToolCall, ToolDefinition


class OllamaClient(BaseLLMClient):
    """Client for local Ollama HTTP API with native and fallback tool calling."""

    def __init__(self, config: ModelConfig):
        self.config = config
        self.api_base = config.api_base.rstrip("/")
        self.client = httpx.Client(base_url=self.api_base, timeout=120.0)

    def is_available(self) -> bool:
        """Check if Ollama server is running and reachable."""
        try:
            resp = self.client.get("/api/tags")
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """List models installed on the local Ollama instance."""
        try:
            resp = self.client.get("/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            pass
        return []

    def generate(
        self,
        messages: List[LLMMessage],
        tools: Optional[List[ToolDefinition]] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Send chat completion request to Ollama /api/chat."""
        payload_messages = []
        for m in messages:
            msg_dict: Dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "tool" and m.name:
                # Ollama tool response
                msg_dict["role"] = "tool"
            payload_messages.append(msg_dict)

        payload: Dict[str, Any] = {
            "model": self.config.name,
            "messages": payload_messages,
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_ctx": self.config.context_window,
            },
        }

        if json_mode:
            payload["format"] = "json"

        # Format tools if provided
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        try:
            resp = self.client.post("/api/chat", json=payload)
            if resp.status_code != 200:
                # If model not found or tools unsupported, try without tools payload
                if resp.status_code == 400 and tools:
                    # Fallback to system prompt tool instructions
                    return self._generate_with_prompt_tools(messages, tools, json_mode)
                return LLMResponse(
                    content="",
                    finish_reason="error",
                    raw_response={"status_code": resp.status_code, "error": resp.text},
                )

            data = resp.json()
            message = data.get("message", {})
            content = message.get("content", "") or ""
            raw_tools = message.get("tool_calls", [])

            tool_calls = []
            for i, tc in enumerate(raw_tools):
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"raw": args}
                tool_calls.append(ToolCall(id=f"call_{i}", name=name, arguments=args))

            # If no native tool calls returned, check for XML/JSON tool patterns in text
            if not tool_calls and tools:
                parsed_calls = self._parse_fallback_tool_calls(content)
                if parsed_calls:
                    tool_calls = parsed_calls

            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason="tool_calls" if tool_calls else "stop",
                raw_response=data,
            )

        except Exception as e:
            return LLMResponse(
                content="",
                finish_reason="error",
                raw_response={"exception": str(e)},
            )

    def _generate_with_prompt_tools(
        self,
        messages: List[LLMMessage],
        tools: List[ToolDefinition],
        json_mode: bool,
    ) -> LLMResponse:
        """Fallback tool execution via system prompt injection when native tool calling is unsupported."""
        tool_desc = json.dumps([t.model_dump() for t in tools], indent=2)
        system_addon = (
            f"\n\nYou have access to these tools:\n{tool_desc}\n\n"
            "To call a tool, output a single JSON block wrapped in ```json\n"
            '{"tool": "tool_name", "arguments": {...}}\n```'
        )

        modified_messages = []
        for m in messages:
            if m.role == "system":
                modified_messages.append({"role": "system", "content": m.content + system_addon})
            else:
                modified_messages.append({"role": m.role, "content": m.content})

        payload = {
            "model": self.config.name,
            "messages": modified_messages,
            "stream": False,
            "options": {"temperature": self.config.temperature},
        }
        resp = self.client.post("/api/chat", json=payload)
        data = resp.json()
        content = data.get("message", {}).get("content", "")
        tool_calls = self._parse_fallback_tool_calls(content)

        return LLMResponse(content=content, tool_calls=tool_calls, raw_response=data)

    def _parse_fallback_tool_calls(self, text: str) -> List[ToolCall]:
        """Extract JSON tool calls from markdown codeblocks or raw JSON in LLM response."""
        calls = []
        json_pattern = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
        for match in json_pattern.finditer(text):
            block = match.group(1).strip()
            try:
                data = json.loads(block)
                if "tool" in data and "arguments" in data:
                    calls.append(ToolCall(name=data["tool"], arguments=data["arguments"]))
                elif "name" in data and "arguments" in data:
                    calls.append(ToolCall(name=data["name"], arguments=data["arguments"]))
            except Exception:
                continue
        return calls
