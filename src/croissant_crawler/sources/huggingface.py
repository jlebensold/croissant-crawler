"""Hugging Face Hub: datasets tagged ``library:mlcroissant`` expose ``/api/datasets/{id}/croissant``."""

from __future__ import annotations

import re
from typing import Any

import orjson

from ..http import PoliteClient
from .base import DiscoveredLink, DiscoveryError, DiscoveryPage, Source

API = "https://huggingface.co/api/datasets"


class HuggingFaceSource(Source):
    id = "huggingface"
    name = "Hugging Face"
    base_url = "https://huggingface.co"
    page_size = 1000

    def croissant_url(self, external_id: str) -> str:
        return f"{API}/{external_id}/croissant"

    def landing_url(self, external_id: str) -> str:
        return f"https://huggingface.co/datasets/{external_id}"

    async def discover_page(self, client: PoliteClient, cursor: dict[str, Any] | None) -> DiscoveryPage:
        if cursor and cursor.get("next_url"):
            res = await client.get(cursor["next_url"], headers={"Accept": "application/json"})
        else:
            params = {
                "filter": "library:mlcroissant",  # only datasets with a Croissant file
                "sort": "downloads",
                "direction": "-1",
                "limit": str(self.page_size),
            }
            res = await client.get(API, params=params, headers={"Accept": "application/json"})
        if not res.ok:
            raise DiscoveryError(f"HF listing failed: HTTP {res.status} {res.error or ''}")
        items = orjson.loads(res.content)
        links = []
        for it in items:
            rid = it.get("id")
            if not rid or it.get("private") or it.get("disabled"):
                continue
            tags = it.get("tags") or []
            links.append(
                DiscoveredLink(
                    external_id=rid,
                    croissant_url=self.croissant_url(rid),
                    landing_url=self.landing_url(rid),
                    source_version=it.get("sha"),
                    popularity=float(it.get("downloads") or 0),
                    meta={
                        "downloads": it.get("downloads"),
                        "likes": it.get("likes"),
                        "gated": it.get("gated"),
                        "author": it.get("author"),
                        "created_at": it.get("createdAt"),
                        "last_modified": it.get("lastModified"),
                        "tags": tags,
                    },
                )
            )
        next_url = _next_link(res.headers.get("link", ""))
        return DiscoveryPage(links, {"next_url": next_url} if next_url and items else None)


def _next_link(header: str) -> str | None:
    for part in header.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None
