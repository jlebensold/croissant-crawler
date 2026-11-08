"""Generic web source: Croissant files (or HTML pages embedding Croissant JSON-LD) from seed URLs.

Seeds come from WEB_SEEDS (comma separated) and from user submissions through the API/CLI.
A seed ending in ``.xml`` is treated as a sitemap and expanded into its ``<loc>`` entries.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..http import PoliteClient
from .base import DiscoveredLink, DiscoveryPage, Source


class WebSource(Source):
    id = "web"
    name = "Web (submitted & seeds)"
    base_url = ""

    def croissant_url(self, external_id: str) -> str:
        return external_id

    def landing_url(self, external_id: str) -> str:
        return external_id

    def seeds(self) -> list[str]:
        return [s.strip() for s in os.getenv("WEB_SEEDS", "").split(",") if s.strip()]

    async def discover_page(self, client: PoliteClient, cursor: dict[str, Any] | None) -> DiscoveryPage:
        links: list[DiscoveredLink] = []
        for seed in self.seeds():
            urls = [seed]
            if seed.endswith(".xml"):
                res = await client.get(seed)
                urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", res.content.decode("utf-8", "replace")) if res.ok else []
            for url in urls:
                links.append(DiscoveredLink(external_id=url, croissant_url=url, landing_url=url))
        return DiscoveryPage(links, None)

    def link_for(self, url: str) -> DiscoveredLink:
        return DiscoveredLink(external_id=url, croissant_url=url, landing_url=url, meta={"submitted": True})
