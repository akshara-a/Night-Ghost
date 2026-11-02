"""Real-model smoke test. Skipped unless an Ollama server is up; run with: pytest -m ollama."""

import pytest

from nightghost.config import ModelConfig
from nightghost.llm.base import LLMMessage
from nightghost.llm.ollama import OllamaClient

pytestmark = pytest.mark.ollama


def test_ollama_round_trip():
    config = ModelConfig()
    client = OllamaClient(config)
    if not client.is_available():
        pytest.skip("Ollama server is not running")
    installed = client.list_models()
    base = config.name.split(":")[0]
    if not any(m.startswith(base) for m in installed):
        pytest.skip(f"Model {config.name} is not installed (ollama pull {config.name})")
    resp = client.generate([LLMMessage(role="user", content="Reply with the single word: ready")])
    assert resp.content.strip()
