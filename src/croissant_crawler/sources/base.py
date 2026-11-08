"""Source adapters turn a data platform's listing API into Croissant links.

Each adapter is a small, stateless object with two responsibilities:

* ``discover_page(client, cursor)``: return one page of links plus the cursor for the next page
  (``None`` when exhausted). Cursors are JSON-serialisable so discovery can be checkpointed in
  Postgres and resumed after a crash or by a Temporal ``continue_as_new``.
* ``croissant_url(external_id)``: build the Croissant URL for an item id.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from ..http import PoliteClient


@dataclass
class DiscoveredLink:
    external_id: str
    croissant_url: str
    landing_url: str | None = None
    source_version: str | None = None  # upstream revision marker; unchanged => skip refetch
    popularity: float = 0.0  # raw popularity signal (downloads, votes ...)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def priority(self) -> float:
        return math.log10(1 + max(self.popularity, 0))


@dataclass
class DiscoveryPage:
    links: list[DiscoveredLink]
    next_cursor: dict[str, Any] | None


class DiscoveryError(RuntimeError):
    pass


class Source(ABC):
    id: str
    name: str
    base_url: str
    page_size: int = 100

    @abstractmethod
    async def discover_page(
        self, client: PoliteClient, cursor: dict[str, Any] | None
    ) -> DiscoveryPage: ...

    @abstractmethod
    def croissant_url(self, external_id: str) -> str: ...

    def landing_url(self, external_id: str) -> str | None:
        return None
