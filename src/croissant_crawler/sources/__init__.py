"""Source registry."""

from __future__ import annotations

from .base import DiscoveredLink, DiscoveryError, DiscoveryPage, Source
from .dataverse import DataverseSource
from .huggingface import HuggingFaceSource
from .kaggle import KaggleSource
from .openml import OpenMLSource
from .web import WebSource

_REGISTRY: dict[str, type[Source]] = {
    "huggingface": HuggingFaceSource,
    "openml": OpenMLSource,
    "kaggle": KaggleSource,
    "dataverse": DataverseSource,
    "web": WebSource,
}


def get_source(source_id: str) -> Source:
    try:
        return _REGISTRY[source_id]()
    except KeyError as exc:
        raise ValueError(f"unknown source {source_id!r}; known: {sorted(_REGISTRY)}") from exc


def all_source_ids() -> list[str]:
    return list(_REGISTRY)


__all__ = [
    "DiscoveredLink",
    "DiscoveryError",
    "DiscoveryPage",
    "Source",
    "get_source",
    "all_source_ids",
]
