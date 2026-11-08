"""Minimal OpenRouter chat client with JSON-schema structured output and model fallbacks."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import orjson

from .config import get_settings
from .http import TokenBucket

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


class OpenRouter:
    def __init__(self):
        s = get_settings()
        if not s.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        self.models = [s.llm_model, *[m for m in s.llm_fallback_models if m and m != s.llm_model]]
        self._sem = asyncio.Semaphore(s.llm_concurrency)
        self._bucket = TokenBucket(s.llm_rate_per_second)
        self._client = httpx.AsyncClient(
            base_url=s.openrouter_base_url,
            timeout=httpx.Timeout(120.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {s.openrouter_api_key}",
                "X-Title": "croissant-crawler",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def structured(self, system: str, user: str, schema_name: str, schema: dict, max_tokens: int = 1200) -> LLMResult:
        body = {
            "model": self.models[0],
            "models": self.models,  # OpenRouter falls back down this list
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            # Only parameters every listed model supports: with require_parameters an unsupported
            # one (e.g. temperature on newer Claude models) silently routes to the fallback.
            "provider": {"require_parameters": True},
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        last: Exception | None = None
        # Walk the model list ourselves: OpenRouter's `models` fallback covers HTTP/provider errors,
        # but a *refusal* (finish_reason=content_filter, e.g. a safety classifier false positive on
        # a microbiology dataset) is a successful response and would never fall through.
        for idx, model in enumerate(self.models):
            body["model"], body["models"] = model, self.models[idx:]
            for attempt in range(3):
                async with self._sem:
                    await self._bucket.acquire()
                    try:
                        r = await self._client.post("/chat/completions", json=body)
                    except httpx.HTTPError as exc:
                        last = exc
                        await asyncio.sleep(2**attempt)
                        continue
                if r.status_code in (429, 500, 502, 503, 504):
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code != 200:
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                    break  # bad request for this model: try the next one
                payload = r.json()
                if "error" in payload:
                    last = LLMError(str(payload["error"])[:500])
                    await asyncio.sleep(2**attempt)
                    continue
                choice = payload["choices"][0]
                content = (choice.get("message") or {}).get("content") or ""
                if choice.get("finish_reason") == "content_filter" or not content.strip():
                    last = LLMError(f"{payload.get('model', model)} returned no content "
                                    f"(finish_reason={choice.get('finish_reason')})")
                    log.info("%s; falling back to next model", last)
                    break
                try:
                    data = orjson.loads(_strip_fences(content))
                except orjson.JSONDecodeError:
                    last = LLMError(f"model returned non-JSON: {content[:200]}")
                    continue
                usage = payload.get("usage") or {}
                return LLMResult(
                    data=data,
                    model=payload.get("model", model),
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    cost_usd=usage.get("cost"),
                )
        raise LLMError(f"LLM call failed for all models: {last}")


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
