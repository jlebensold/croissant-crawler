"""Dataverse installations (6.x+) ship a Croissant exporter:
``/api/datasets/export?exporter=croissant&persistentId=<doi>``.

Several instances can be configured (DATAVERSE_INSTANCES); discovery walks them in order.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import orjson

from ..config import get_settings
from ..http import PoliteClient
from .base import DiscoveredLink, DiscoveryError, DiscoveryPage, Source


class DataverseSource(Source):
    id = "dataverse"
    name = "Dataverse"
    base_url = "https://dataverse.org"
    page_size = 100

    def __init__(self, instances: list[str] | None = None):
        self.instances = [i.rstrip("/") for i in (instances or get_settings().dataverse_instances)]

    @staticmethod
    def _split(external_id: str) -> tuple[str, str]:
        # external ids look like "https://dataverse.harvard.edu|doi:10.7910/DVN/TJCLKP"
        base, _, pid = external_id.partition("|")
        return base, pid

    def croissant_url(self, external_id: str) -> str:
        base, pid = self._split(external_id)
        return f"{base}/api/datasets/export?exporter=croissant&persistentId={quote(pid, safe=':/')}"

    def landing_url(self, external_id: str) -> str:
        base, pid = self._split(external_id)
        return f"{base}/dataset.xhtml?persistentId={pid}"

    async def discover_page(self, client: PoliteClient, cursor: dict[str, Any] | None) -> DiscoveryPage:
        cursor = cursor or {"instance": 0, "start": 0}
        idx, start = int(cursor.get("instance", 0)), int(cursor.get("start", 0))
        if idx >= len(self.instances):
            return DiscoveryPage([], None)
        base = self.instances[idx]
        res = await client.get(
            f"{base}/api/search",
            params={
                "q": "*",
                "type": "dataset",
                "per_page": str(self.page_size),
                "start": str(start),
                # Datasets with files are the ones worth describing with Croissant.
                "fq": "fileCount:[1 TO *]",
                "sort": "date",
                "order": "desc",
            },
            headers={"Accept": "application/json"},
        )
        if not res.ok:
            raise DiscoveryError(f"Dataverse listing failed ({base}): HTTP {res.status}")
        data = orjson.loads(res.content).get("data", {})
        items = data.get("items", [])
        links = []
        for it in items:
            pid = it.get("global_id")
            if not pid:
                continue
            ext = f"{base}|{pid}"
            links.append(
                DiscoveredLink(
                    external_id=ext,
                    croissant_url=self.croissant_url(ext),
                    landing_url=it.get("url") or self.landing_url(ext),
                    source_version=str(it.get("versionId") or ""),
                    popularity=0.0,
                    meta={
                        "instance": base,
                        "name": it.get("name"),
                        "publisher": it.get("publisher"),
                        "subjects": it.get("subjects"),
                        "file_count": it.get("fileCount"),
                        "published_at": it.get("published_at"),
                        "updated_at": it.get("updatedAt"),
                    },
                )
            )
        total = int(data.get("total_count") or 0)
        nxt = start + len(items)
        if items and nxt < total:
            return DiscoveryPage(links, {"instance": idx, "start": nxt})
        if idx + 1 < len(self.instances):
            return DiscoveryPage(links, {"instance": idx + 1, "start": 0})
        return DiscoveryPage(links, None)
