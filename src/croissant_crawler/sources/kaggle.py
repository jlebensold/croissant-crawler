"""Kaggle: public datasets expose ``/datasets/{owner}/{slug}/croissant/download``.

The anonymous listing API returns 20 items per page; set KAGGLE_USERNAME/KAGGLE_KEY for
higher limits (sent as HTTP basic auth).
"""

from __future__ import annotations

import base64
import os
from typing import Any

import orjson

from ..http import PoliteClient
from .base import DiscoveredLink, DiscoveryError, DiscoveryPage, Source

LIST_API = "https://www.kaggle.com/api/v1/datasets/list"


class KaggleSource(Source):
    id = "kaggle"
    name = "Kaggle"
    base_url = "https://www.kaggle.com"
    page_size = 20
    max_pages = 500  # Kaggle stops paging around here

    def __init__(self, sort_by: str = "votes"):
        self.sort_by = sort_by

    def croissant_url(self, external_id: str) -> str:
        return f"https://www.kaggle.com/datasets/{external_id}/croissant/download"

    def landing_url(self, external_id: str) -> str:
        return f"https://www.kaggle.com/datasets/{external_id}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        user, key = os.getenv("KAGGLE_USERNAME"), os.getenv("KAGGLE_KEY")
        if user and key:
            token = base64.b64encode(f"{user}:{key}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        return headers

    async def discover_page(self, client: PoliteClient, cursor: dict[str, Any] | None) -> DiscoveryPage:
        page = int((cursor or {}).get("page", 1))
        res = await client.get(
            LIST_API, params={"page": str(page), "sortBy": self.sort_by}, headers=self._headers()
        )
        if not res.ok:
            raise DiscoveryError(f"Kaggle listing failed: HTTP {res.status} {res.error or ''}")
        rows = orjson.loads(res.content) or []
        links = []
        for row in rows:
            ref = row.get("ref")
            if not ref or row.get("isPrivate"):
                continue
            links.append(
                DiscoveredLink(
                    external_id=ref,
                    croissant_url=self.croissant_url(ref),
                    landing_url=self.landing_url(ref),
                    source_version=str(row.get("currentVersionNumber") or ""),
                    popularity=float(row.get("downloadCount") or 0),
                    meta={
                        "title": row.get("title"),
                        "downloads": row.get("downloadCount"),
                        "votes": row.get("voteCount"),
                        "views": row.get("viewCount"),
                        "usability": row.get("usabilityRating"),
                        "license": row.get("licenseName"),
                        "total_bytes": row.get("totalBytes"),
                        "last_updated": row.get("lastUpdated"),
                        "tags": [t.get("name") for t in row.get("tags") or [] if t.get("name")],
                    },
                )
            )
        has_more = len(rows) > 0 and page < self.max_pages
        return DiscoveryPage(links, {"page": page + 1} if has_more else None)
