"""Workflow inputs (plain dataclasses, safe to import inside the Temporal sandbox)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAIN_QUEUE = "croissant-main"
LLM_QUEUE = "croissant-llm"


def fetch_queue(source_id: str) -> str:
    return f"croissant-fetch-{source_id}"


@dataclass
class SourceCrawlInput:
    source_id: str
    limit: int = 1000
    discover: bool = True
    batch_size: int = 50
    # continuation state (continue-as-new)
    stage: str = "discover"  # discover | fetch
    cursor: dict[str, Any] | None = None
    seen: int = 0
    processed: int = 0
    stats: dict[str, int] = field(default_factory=dict)


@dataclass
class EnrichInput:
    limit: int = 1000
    batch_size: int = 50
    done: int = 0
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineInput:
    sources: list[str]
    limit: int = 1000
    discover: bool = True
    enrich: bool = True
    graph: bool = True
