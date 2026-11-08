"""Temporal activities: thin adapters over ``pipeline.*`` services (IDs in, small dicts out)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from temporalio import activity

from ..llm import OpenRouter
from ..pipeline import crawl, enrich, graph, index, store

_llm: OpenRouter | None = None


@activity.defn
async def ensure_sources() -> None:
    await store.ensure_sources()


@activity.defn
async def discover_page(source_id: str, cursor: dict[str, Any] | None) -> dict[str, Any]:
    return asdict(await crawl.discover_page(source_id, cursor))


@activity.defn
async def lease_links(source_id: str, limit: int) -> list[int]:
    return await store.lease_due_links(source_id, limit)


@activity.defn
async def fetch_link(link_id: int) -> dict[str, Any]:
    return (await crawl.fetch_link(link_id)).to_dict()


@activity.defn
async def process_documents(document_ids: list[int]) -> list[dict[str, Any]]:
    out = []
    for i, doc_id in enumerate(document_ids):
        res = await crawl.process_document(doc_id)
        if res:
            out.append(res.to_dict())
        activity.heartbeat(i)
    return out


@activity.defn
async def index_links(link_ids: list[int]) -> dict[str, Any]:
    return await index.index_links(link_ids)


@activity.defn
async def index_pending() -> dict[str, Any]:
    return await index.index_pending()


@activity.defn
async def select_for_enrichment(limit: int) -> list[int]:
    return await enrich.links_needing_enrichment(limit)


@activity.defn
async def enrich_link(link_id: int) -> dict[str, Any]:
    global _llm
    if _llm is None:
        _llm = OpenRouter()
    return await enrich.enrich_link(link_id, _llm)


@activity.defn
async def compute_similarities() -> dict[str, Any]:
    task = asyncio.create_task(graph.compute_similarities())
    while not task.done():  # keep the activity alive while the long query loop runs
        activity.heartbeat()
        await asyncio.sleep(5)
    return task.result()


@activity.defn
async def rebuild_graph() -> dict[str, Any]:
    return await graph.rebuild_graph()


MAIN_ACTIVITIES = [ensure_sources, discover_page, lease_links, process_documents, index_links,
                   index_pending, select_for_enrichment, compute_similarities, rebuild_graph]
FETCH_ACTIVITIES = [fetch_link]
LLM_ACTIVITIES = [enrich_link]
