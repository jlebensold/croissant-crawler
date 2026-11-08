"""In-process orchestrator (no Temporal). Same services, simple asyncio fan-out.

Useful for development, tests, CI and one-off backfills. Production runs use the Temporal
workflows in ``orchestration.workflows`` which add durability, schedules and visibility.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any

from ..pipeline import crawl, enrich, graph, index, store

log = logging.getLogger(__name__)


async def crawl_source(source_id: str, limit: int, *, discover: bool = True, concurrency: int = 8,
                       batch: int = 50, progress=None) -> dict[str, Any]:
    await store.ensure_sources()
    stats: Counter = Counter()
    if discover:
        d = await crawl.discover(source_id, max_links=limit)
        stats.update({f"discover_{k}": v for k, v in d.items()})
    sem = asyncio.Semaphore(concurrency)

    async def one(link_id: int) -> dict:
        async with sem:
            return await crawl.crawl_link(link_id)

    processed = 0
    while processed < limit:
        ids = await store.lease_due_links(source_id, min(batch, limit - processed))
        if not ids:
            break
        results = await asyncio.gather(*(one(i) for i in ids), return_exceptions=True)
        indexable = []
        for link_id, res in zip(ids, results, strict=True):
            if isinstance(res, BaseException):
                stats["exceptions"] += 1
                log.exception("crawl failed for %s", link_id, exc_info=res)
                continue
            stats[f"fetch_{res['fetch']['status']}"] += 1
            if res.get("process"):
                stats[f"validation_{res['process']['status']}"] += 1
                indexable.append(link_id)
        processed += len(ids)
        deferred = sum(1 for r in results if not isinstance(r, BaseException) and r["fetch"]["status"] == "deferred")
        if deferred >= max(3, len(ids) // 2):
            log.warning("%s looks unhealthy (%d/%d deferred); stopping this source for now", source_id, deferred, len(ids))
            stats["stopped_unhealthy"] = 1
        if indexable:
            try:
                r = await index.index_links(indexable)
                stats["indexed"] += r["indexed"]
            except Exception as exc:  # search being down must not stop the crawl
                log.warning("indexing failed (will retry via index_pending): %s", exc)
        if progress:
            progress(source_id, processed, dict(stats))
        if stats.get("stopped_unhealthy"):
            break
    return dict(stats)


async def enrich_pending(limit: int, concurrency: int = 12, progress=None) -> dict[str, Any]:
    """Continuous worker pool (no per-batch barrier); index flushes every 50 completions."""
    from ..llm import OpenRouter

    ids = await enrich.links_needing_enrichment(limit)
    client = OpenRouter()
    stats: Counter = Counter()
    cost = 0.0
    sem = asyncio.Semaphore(concurrency)
    to_index: list[int] = []

    async def one(i: int) -> dict:
        async with sem:
            return await enrich.enrich_link(i, client)

    try:
        tasks = [asyncio.create_task(one(i)) for i in ids]
        for n, fut in enumerate(asyncio.as_completed(tasks), 1):
            r = await fut
            stats[r["status"]] += 1
            cost += r.get("cost_usd") or 0.0
            if r["status"] == "ok":
                to_index.append(r["link_id"])
            if len(to_index) >= 50 or n == len(tasks):
                batch, to_index = to_index, []
                try:
                    await index.index_links(batch)
                except Exception as exc:  # index_pending will catch up
                    log.warning("index flush failed: %s", exc)
                if progress:
                    progress("enrich", n, dict(stats))
    finally:
        await client.aclose()
    return {**stats, "cost_usd": round(cost, 4)}


async def run_pipeline(sources: list[str], limit: int, *, do_enrich: bool, do_graph: bool,
                       progress=None) -> dict[str, Any]:
    results: dict[str, Any] = {}
    crawled = await asyncio.gather(*(crawl_source(s, limit, progress=progress) for s in sources),
                                   return_exceptions=True)
    for s, r in zip(sources, crawled, strict=True):
        results[s] = r if not isinstance(r, BaseException) else {"error": repr(r)}
    results["index"] = await index.index_pending()
    if do_enrich:
        results["enrich"] = await enrich_pending(limit * len(sources), progress=progress)
        results["index_after_enrich"] = await index.index_pending()
    if do_graph:
        results["similarities"] = await graph.compute_similarities()
        results["graph"] = await graph.rebuild_graph()
    return results
