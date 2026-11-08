"""OpenML: every active dataset has a Croissant export at ``/croissant/dataset/{id}``."""

from __future__ import annotations

from typing import Any

import orjson

from ..http import PoliteClient
from .base import DiscoveredLink, DiscoveryError, DiscoveryPage, Source

LIST_API = "https://www.openml.org/api/v1/json/data/list/limit/{limit}/offset/{offset}/status/active"


class OpenMLSource(Source):
    id = "openml"
    name = "OpenML"
    base_url = "https://www.openml.org"
    page_size = 1000

    def croissant_url(self, external_id: str) -> str:
        return f"https://www.openml.org/croissant/dataset/{external_id}"

    def landing_url(self, external_id: str) -> str:
        return f"https://www.openml.org/search?type=data&id={external_id}"

    async def discover_page(self, client: PoliteClient, cursor: dict[str, Any] | None) -> DiscoveryPage:
        offset = int((cursor or {}).get("offset", 0))
        res = await client.get(
            LIST_API.format(limit=self.page_size, offset=offset),
            headers={"Accept": "application/json"},
        )
        if res.status == 412:  # OpenML signals "no more results" with 412 + code 372
            return DiscoveryPage([], None)
        if not res.ok:
            raise DiscoveryError(f"OpenML listing failed: HTTP {res.status} {res.error or ''}")
        rows = orjson.loads(res.content).get("data", {}).get("dataset", [])
        links = []
        for row in rows:
            did = str(row.get("did"))
            qualities = {q["name"]: q.get("value") for q in row.get("quality", []) if "name" in q}
            n_inst = _num(qualities.get("NumberOfInstances"))
            links.append(
                DiscoveredLink(
                    external_id=did,
                    croissant_url=self.croissant_url(did),
                    landing_url=self.landing_url(did),
                    source_version=f"v{row.get('version')}:{row.get('md5_checksum') or row.get('file_id')}",
                    # OpenML's listing has no download counts; use dataset size as a weak signal.
                    popularity=0.0,
                    meta={
                        "name": row.get("name"),
                        "version": row.get("version"),
                        "format": row.get("format"),
                        "instances": n_inst,
                        "features": _num(qualities.get("NumberOfFeatures")),
                        "classes": _num(qualities.get("NumberOfClasses")),
                    },
                )
            )
        next_cursor = {"offset": offset + len(rows)} if len(rows) == self.page_size else None
        return DiscoveryPage(links, next_cursor)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
