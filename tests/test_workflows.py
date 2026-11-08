"""Workflow logic tests on Temporal's time-skipping test server, with fake activities."""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from croissant_crawler.orchestration.types import (
    LLM_QUEUE,
    MAIN_QUEUE,
    PipelineInput,
    SourceCrawlInput,
    fetch_queue,
)
from croissant_crawler.orchestration.workflows import (
    WORKFLOWS,
    PipelineWorkflow,
    SourceCrawlWorkflow,
)

pytestmark = pytest.mark.network  # first run downloads the Temporal test server


class Fake:
    def __init__(self, n_links=7, fetch_status="ok"):
        self.frontier = list(range(1, n_links + 1))
        self.fetch_status = fetch_status
        self.processed: list[int] = []
        self.indexed: list[int] = []
        self.pages = 0

    def activities(self):
        @activity.defn(name="ensure_sources")
        async def ensure_sources() -> None:
            return None

        @activity.defn(name="discover_page")
        async def discover_page(source_id: str, cursor):
            self.pages += 1
            page = (cursor or {}).get("p", 0)
            nxt = {"p": page + 1} if page < 2 else None
            return {"source_id": source_id, "links_seen": 3, "links_new": 3, "next_cursor": nxt}

        @activity.defn(name="lease_links")
        async def lease_links(source_id: str, limit: int):
            out, self.frontier = self.frontier[:limit], self.frontier[limit:]
            return out

        @activity.defn(name="fetch_link")
        async def fetch_link(link_id: int):
            ok = self.fetch_status == "ok"
            return {"link_id": link_id, "status": self.fetch_status, "changed": ok,
                    "document_id": 100 + link_id if ok else None, "needs_processing": ok}

        @activity.defn(name="process_documents")
        async def process_documents(doc_ids):
            self.processed += doc_ids
            return [{"document_id": d, "link_id": d - 100, "status": "valid", "quality_score": 80, "quality_tier": "A"} for d in doc_ids]

        @activity.defn(name="index_links")
        async def index_links(ids):
            self.indexed += ids
            return {"indexed": len(ids), "errors": []}

        @activity.defn(name="index_pending")
        async def index_pending():
            return {"indexed": 0}

        @activity.defn(name="select_for_enrichment")
        async def select_for_enrichment(limit: int):
            return []

        @activity.defn(name="compute_similarities")
        async def compute_similarities():
            return {"computed": 0}

        @activity.defn(name="rebuild_graph")
        async def rebuild_graph():
            return {"nodes": 1, "edges": 0}

        @activity.defn(name="enrich_link")
        async def enrich_link(i: int):
            return {"status": "ok"}

        main = [ensure_sources, discover_page, lease_links, process_documents, index_links, index_pending,
                select_for_enrichment, compute_similarities, rebuild_graph]
        return main, [fetch_link], [enrich_link]


async def _run(fake: Fake, fn, arg, source="huggingface"):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        main, fetch, llm = fake.activities()
        async with Worker(env.client, task_queue=MAIN_QUEUE, workflows=WORKFLOWS, activities=main), \
                   Worker(env.client, task_queue=fetch_queue(source), activities=fetch), \
                   Worker(env.client, task_queue=LLM_QUEUE, activities=llm):
            return await env.client.execute_workflow(fn, arg, id=str(uuid.uuid4()), task_queue=MAIN_QUEUE)


async def test_source_crawl_discovers_fetches_processes_indexes():
    fake = Fake(n_links=7)
    stats = await _run(fake, SourceCrawlWorkflow.run, SourceCrawlInput(source_id="huggingface", limit=100, batch_size=3))
    assert fake.pages == 3 and stats["discovered"] == 9
    assert stats["fetch_ok"] == 7 and stats["processed"] == 7
    assert sorted(fake.indexed) == list(range(1, 8))
    assert stats["validation_valid"] == 7


async def test_source_crawl_respects_limit():
    fake = Fake(n_links=20)
    stats = await _run(fake, SourceCrawlWorkflow.run, SourceCrawlInput(source_id="huggingface", limit=5, batch_size=4, discover=False))
    assert stats["processed"] == 5 and len(fake.frontier) == 15


async def test_unhealthy_source_stops_early():
    fake = Fake(n_links=50, fetch_status="deferred")
    stats = await _run(fake, SourceCrawlWorkflow.run, SourceCrawlInput(source_id="huggingface", limit=50, batch_size=10, discover=False))
    assert stats.get("stopped_unhealthy") == 1 and stats["processed"] == 10
    assert fake.processed == []


async def test_pipeline_runs_children_and_graph():
    fake = Fake(n_links=4)
    out = await _run(fake, PipelineWorkflow.run, PipelineInput(sources=["huggingface"], limit=10, enrich=True, graph=True))
    assert out["huggingface"]["processed"] == 4
    assert out["graph"]["graph"] == {"nodes": 1, "edges": 0}
    assert out["enrich"] == {}
