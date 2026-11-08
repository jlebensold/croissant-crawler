import json

import httpx
import pytest
import respx

from croissant_crawler import config
from croissant_crawler.llm import LLMError, OpenRouter

URL = "https://openrouter.ai/api/v1/chat/completions"


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "primary/model")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", '["fallback/model"]')
    monkeypatch.setenv("LLM_RATE_PER_SECOND", "1000")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _reply(model, content, finish="stop"):
    return httpx.Response(200, json={"model": model, "choices": [{"finish_reason": finish, "message": {"content": content}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}})


@respx.mock
async def test_refusal_falls_back_to_next_model(llm_env):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["model"])
        assert "temperature" not in body  # would silently disqualify some models under require_parameters
        if body["model"] == "primary/model":
            return _reply("primary/model", None, finish="content_filter")
        return _reply("fallback/model", '```json\n{"ok": true}\n```')

    respx.post(URL).mock(side_effect=handler)
    client = OpenRouter()
    res = await client.structured("sys", "user", "x", {"type": "object"})
    await client.aclose()
    assert seen == ["primary/model", "fallback/model"]
    assert res.data == {"ok": True} and res.model == "fallback/model" and res.cost_usd == 0.001


@respx.mock
async def test_all_models_failing_raises(llm_env):
    respx.post(URL).mock(return_value=_reply("m", "", finish="content_filter"))
    client = OpenRouter()
    with pytest.raises(LLMError):
        await client.structured("sys", "user", "x", {"type": "object"})
    await client.aclose()
