"""Temporal workflows.

PipelineWorkflow
  ├─ SourceCrawlWorkflow (one child per source, in parallel)
  │     discover pages (checkpointed cursor) → lease due links → fetch (per-source rate-limited
  │     task queue) → validate/extract → index; continue-as-new keeps history small
  ├─ index_pending (catch-up)
  ├─ EnrichWorkflow  (LLM queue, rate limited; cached by content hash)
  └─ compute_similarities → rebuild_graph
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from . import activities as A
    from .types import (
        LLM_QUEUE,
        EnrichInput,
        PipelineInput,
        SourceCrawlInput,
        fetch_queue,
    )

RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                    maximum_interval=timedelta(minutes=2), maximum_attempts=5)
HISTORY_LIMIT = 6000  # events before continue-as-new


def _bump(stats: dict[str, Any], key: str, n: int = 1) -> None:
    stats[key] = stats.get(key, 0) + n


@workflow.defn
class SourceCrawlWorkflow:
    async def _post_process(self, document_ids: list[int], stats: dict[str, Any]) -> None:
        results = await workflow.execute_activity(
            A.process_documents, document_ids, start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2), retry_policy=RETRY)
        for r in results:
            _bump(stats, f"validation_{r['status']}")
        link_ids = [r["link_id"] for r in results if r["status"] != "not_croissant"]
        if not link_ids:
            return
        try:
            idx = await workflow.execute_activity(
                A.index_links, link_ids, start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3))
            _bump(stats, "indexed", idx["indexed"])
        except Exception:  # search outage: index_pending catches up later
            _bump(stats, "index_failed", len(link_ids))

    @workflow.run
    async def run(self, inp: SourceCrawlInput) -> dict[str, Any]:
        stats = dict(inp.stats)
        if inp.discover and inp.stage == "discover":
            cursor, seen = inp.cursor, inp.seen
            while True:
                res = await workflow.execute_activity(
                    A.discover_page, args=[inp.source_id, cursor],
                    start_to_close_timeout=timedelta(minutes=5), retry_policy=RETRY)
                seen += res["links_seen"]
                _bump(stats, "discovered", res["links_seen"])
                _bump(stats, "new_links", res["links_new"])
                cursor = res["next_cursor"]
                if cursor is None or seen >= inp.limit:
                    break
                if workflow.info().get_current_history_length() > HISTORY_LIMIT:
                    workflow.continue_as_new(SourceCrawlInput(**{**inp.__dict__, "cursor": cursor, "seen": seen, "stats": stats}))
            inp.stage = "fetch"

        processed = inp.processed
        post: asyncio.Task | None = None  # validate+index of the previous batch, overlapping the next fetches
        while processed < inp.limit:
            ids: list[int] = await workflow.execute_activity(
                A.lease_links, args=[inp.source_id, min(inp.batch_size, inp.limit - processed)],
                start_to_close_timeout=timedelta(minutes=1), retry_policy=RETRY)
            if not ids:
                break
            outcomes = await asyncio.gather(*[
                workflow.execute_activity(
                    A.fetch_link, link_id, task_queue=fetch_queue(inp.source_id),
                    # bounded by the HTTP client's retry budget; short so a dead worker is noticed fast
                    start_to_close_timeout=timedelta(minutes=4), retry_policy=RETRY)
                for link_id in ids
            ], return_exceptions=True)
            to_process, deferred = [], 0
            for o in outcomes:
                if isinstance(o, BaseException):
                    _bump(stats, "fetch_failed")
                    continue
                _bump(stats, f"fetch_{o['status']}")
                deferred += o["status"] == "deferred"
                if o.get("needs_processing") and o.get("document_id"):
                    to_process.append(o["document_id"])
            if post is not None:
                await post
            post = asyncio.create_task(self._post_process(to_process, stats)) if to_process else None
            processed += len(ids)
            if deferred >= max(3, len(ids) // 2):
                stats["stopped_unhealthy"] = 1
                workflow.logger.warning("source %s unhealthy, stopping crawl", inp.source_id)
                break
            if workflow.info().get_current_history_length() > HISTORY_LIMIT:
                if post is not None:
                    await post
                workflow.continue_as_new(SourceCrawlInput(**{**inp.__dict__, "stage": "fetch", "processed": processed, "stats": stats}))
        if post is not None:
            await post
        stats["processed"] = processed
        return stats


@workflow.defn
class EnrichWorkflow:
    @workflow.run
    async def run(self, inp: EnrichInput) -> dict[str, Any]:
        stats = dict(inp.stats)
        done = inp.done
        while done < inp.limit:
            ids: list[int] = await workflow.execute_activity(
                A.select_for_enrichment, min(inp.batch_size, inp.limit - done),
                start_to_close_timeout=timedelta(minutes=1), retry_policy=RETRY)
            if not ids:
                break
            results = await asyncio.gather(*[
                workflow.execute_activity(
                    A.enrich_link, i, task_queue=LLM_QUEUE, start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)))
                for i in ids
            ], return_exceptions=True)
            ok = []
            for i, r in zip(ids, results, strict=True):
                status = "failed" if isinstance(r, BaseException) else r["status"]
                _bump(stats, status)
                if status == "ok":
                    ok.append(i)
                    stats["cost_usd"] = round(stats.get("cost_usd", 0) + (r.get("cost_usd") or 0), 5)
            if ok:
                await workflow.execute_activity(A.index_links, ok, start_to_close_timeout=timedelta(minutes=3),
                                                retry_policy=RETRY)
            done += len(ids)
            if not ok and stats.get("error", 0) + stats.get("failed", 0) >= len(ids):
                raise ApplicationError("every enrichment in the batch failed; check OPENROUTER_API_KEY / model")
            if workflow.info().get_current_history_length() > HISTORY_LIMIT:
                workflow.continue_as_new(EnrichInput(limit=inp.limit, batch_size=inp.batch_size, done=done, stats=stats))
        return stats


@workflow.defn
class GraphWorkflow:
    @workflow.run
    async def run(self) -> dict[str, Any]:
        sims = await workflow.execute_activity(
            A.compute_similarities, start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=1), retry_policy=RETRY)
        g = await workflow.execute_activity(A.rebuild_graph, start_to_close_timeout=timedelta(minutes=30),
                                            retry_policy=RETRY)
        return {"similarities": sims, "graph": g}


@workflow.defn
class PipelineWorkflow:
    def __init__(self) -> None:
        self.phase = "starting"

    @workflow.query
    def current_phase(self) -> str:
        return self.phase

    @workflow.run
    async def run(self, inp: PipelineInput) -> dict[str, Any]:
        out: dict[str, Any] = {}
        await workflow.execute_activity(A.ensure_sources, start_to_close_timeout=timedelta(minutes=1), retry_policy=RETRY)
        self.phase = "crawling"
        wid = workflow.info().workflow_id
        children = await asyncio.gather(*[
            workflow.execute_child_workflow(
                SourceCrawlWorkflow.run,
                SourceCrawlInput(source_id=s, limit=inp.limit, discover=inp.discover),
                id=f"{wid}/crawl-{s}")
            for s in inp.sources
        ], return_exceptions=True)
        for s, res in zip(inp.sources, children, strict=True):
            out[s] = {"error": str(res)} if isinstance(res, BaseException) else res
        self.phase = "indexing"
        out["index"] = await workflow.execute_activity(A.index_pending, start_to_close_timeout=timedelta(minutes=30),
                                                       retry_policy=RETRY)
        if inp.enrich:
            self.phase = "enriching"
            try:
                out["enrich"] = await workflow.execute_child_workflow(
                    EnrichWorkflow.run, EnrichInput(limit=inp.limit * max(1, len(inp.sources))), id=f"{wid}/enrich")
            except Exception as exc:  # enrichment is optional; never fail the crawl because of it
                out["enrich"] = {"error": str(exc)}
        if inp.graph:
            self.phase = "graph"
            out["graph"] = await workflow.execute_child_workflow(GraphWorkflow.run, id=f"{wid}/graph")
        self.phase = "done"
        return out


WORKFLOWS = [PipelineWorkflow, SourceCrawlWorkflow, EnrichWorkflow, GraphWorkflow]
