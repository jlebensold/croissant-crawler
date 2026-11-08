"""Thin async Typesense client + the ``datasets`` collection schema."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import orjson

from .config import get_settings

log = logging.getLogger(__name__)

FACETS = ["source", "license", "tasks", "modalities", "domains", "quality_tier",
          "validation_status", "croissant_version", "languages", "formats"]


def collection_schema(name: str, embedding_model: str) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [
        {"name": "name", "type": "string"},
        {"name": "alternate_names", "type": "string[]", "optional": True},
        {"name": "description", "type": "string"},
        {"name": "summary", "type": "string", "optional": True},
        {"name": "keywords", "type": "string[]", "facet": True, "optional": True},
        {"name": "creators", "type": "string[]", "facet": True, "optional": True},
        {"name": "organizations", "type": "string[]", "facet": True, "optional": True},
        {"name": "source", "type": "string", "facet": True},
        {"name": "external_id", "type": "string"},
        {"name": "license", "type": "string", "facet": True},
        {"name": "tasks", "type": "string[]", "facet": True, "optional": True},
        {"name": "modalities", "type": "string[]", "facet": True, "optional": True},
        {"name": "domains", "type": "string[]", "facet": True, "optional": True},
        {"name": "languages", "type": "string[]", "facet": True, "optional": True},
        {"name": "formats", "type": "string[]", "facet": True, "optional": True},
        {"name": "croissant_version", "type": "string", "facet": True, "optional": True},
        {"name": "validation_status", "type": "string", "facet": True},
        {"name": "quality_tier", "type": "string", "facet": True},
        {"name": "quality_score", "type": "float"},
        {"name": "popularity", "type": "float"},
        {"name": "rank_score", "type": "float"},
        {"name": "is_placeholder", "type": "bool", "facet": True},
        {"name": "enriched", "type": "bool", "facet": True},
        {"name": "date_published", "type": "int64", "optional": True},
        {"name": "updated_at", "type": "int64"},
        {"name": "num_fields", "type": "int32"},
        {"name": "num_files", "type": "int32"},
        {"name": "url", "type": "string", "index": False, "optional": True},
    ]
    if embedding_model:
        fields.append({
            "name": "embedding",
            "type": "float[]",
            "embed": {
                "from": ["name", "summary", "keywords", "description"],
                "model_config": {"model_name": embedding_model},
            },
        })
    return {"name": name, "fields": fields, "default_sorting_field": "rank_score",
            "token_separators": ["-", "_", "/"]}


class Typesense:
    def __init__(self, url: str | None = None, api_key: str | None = None, collection: str | None = None):
        s = get_settings()
        self.collection = collection or s.typesense_collection
        self.embedding_model = s.typesense_embedding_model
        self._client = httpx.AsyncClient(
            base_url=(url or s.typesense_url).rstrip("/"),
            headers={"X-TYPESENSE-API-KEY": api_key or s.typesense_api_key},
            timeout=httpx.Timeout(300.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Typesense:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def health(self) -> bool:
        try:
            r = await self._client.get("/health")
            return r.status_code == 200 and r.json().get("ok", False)
        except httpx.HTTPError:
            return False

    async def collection_info(self) -> dict | None:
        r = await self._client.get(f"/collections/{self.collection}")
        return r.json() if r.status_code == 200 else None

    async def ensure_collection(self, recreate: bool = False) -> None:
        exists = await self.collection_info() is not None
        if exists and recreate:
            await self._client.delete(f"/collections/{self.collection}")
            exists = False
        if not exists:
            r = await self._client.post("/collections", json=collection_schema(self.collection, self.embedding_model))
            if r.status_code not in (200, 201, 409):
                raise RuntimeError(f"cannot create collection: {r.status_code} {r.text}")

    async def upsert(self, docs: list[dict]) -> tuple[int, list[str]]:
        if not docs:
            return 0, []
        body = b"\n".join(orjson.dumps(d) for d in docs)
        r = await self._client.post(
            f"/collections/{self.collection}/documents/import",
            params={"action": "upsert", "batch_size": "50"},
            content=body,
            headers={"Content-Type": "text/plain"},
        )
        r.raise_for_status()
        ok, errors = 0, []
        for line in r.text.splitlines():
            res = orjson.loads(line)
            if res.get("success"):
                ok += 1
            else:
                errors.append(f"{res.get('document', '')[:80]}: {res.get('error')}")
        return ok, errors

    async def delete(self, doc_id: str) -> None:
        await self._client.delete(f"/collections/{self.collection}/documents/{doc_id}")

    async def search(self, params: dict[str, Any]) -> dict:
        # multi_search avoids URL-length limits and supports vector queries cleanly
        body = {"searches": [{"collection": self.collection, **params}]}
        r = await self._client.post("/multi_search", json=body)
        r.raise_for_status()
        result = r.json()["results"][0]
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    async def get(self, doc_id: str) -> dict | None:
        r = await self._client.get(f"/collections/{self.collection}/documents/{doc_id}")
        return r.json() if r.status_code == 200 else None
