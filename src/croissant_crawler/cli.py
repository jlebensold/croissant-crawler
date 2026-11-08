"""Command-line interface: ``croissant-crawler --help``."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import orjson
import typer

from .config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Crawl, validate, enrich and index Croissant datasets.")
wf = typer.Typer(no_args_is_help=True, help="Run the pipeline durably on Temporal.")
app.add_typer(wf, name="workflow")


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "absl", "temporalio.activity", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run(coro):
    async def wrapper():
        from . import db
        try:
            return await coro
        finally:
            await db.close_pool()
    return asyncio.run(wrapper())


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _progress(name: str, n: int, stats: dict) -> None:
    typer.echo(f"[{name}] {n} processed: {stats}", err=True)


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    _setup_logging()
    from . import db
    from .pipeline import store

    async def go():
        applied = await db.migrate()
        await store.ensure_sources()
        return applied
    _print({"applied": _run(go())})


@app.command()
def discover(source: str, limit: Annotated[int | None, typer.Option(help="stop after N links")] = None,
             resume: bool = False) -> None:
    """Enumerate a source's listing and upsert links into the frontier."""
    _setup_logging()
    from .pipeline import crawl, store

    async def go():
        await store.ensure_sources()
        return await crawl.discover(source, max_links=limit, resume=resume)
    _print(_run(go()))


@app.command()
def run(
    sources: Annotated[list[str] | None, typer.Argument(help="sources to crawl (default: SOURCES_ENABLED)")] = None,
    limit: Annotated[int, typer.Option(help="max links to crawl per source")] = 250,
    enrich: Annotated[bool, typer.Option(help="LLM-enrich new/changed datasets")] = True,
    graph: Annotated[bool, typer.Option(help="rebuild the knowledge graph")] = True,
) -> None:
    """Run the full pipeline in-process (no Temporal): discover → fetch → validate → index → enrich → graph."""
    _setup_logging()
    from . import db
    from .orchestration.local import run_pipeline

    srcs = sources or get_settings().sources_enabled

    async def go():
        await db.migrate()
        do_enrich = enrich and bool(get_settings().openrouter_api_key)
        return await run_pipeline(srcs, limit, do_enrich=do_enrich, do_graph=graph, progress=_progress)
    _print(_run(go()))


@app.command()
def index(rebuild: Annotated[bool, typer.Option(help="drop and recreate the collection")] = False) -> None:
    """Push new/changed datasets to Typesense."""
    _setup_logging()
    from .pipeline import index as idx

    _print(_run(idx.reindex_all() if rebuild else idx.index_pending()))


@app.command()
def enrich(limit: int = 500, concurrency: int = 12) -> None:
    """LLM-enrich datasets lacking an enrichment for their current document."""
    _setup_logging()
    from .orchestration.local import enrich_pending

    _print(_run(enrich_pending(limit, concurrency, progress=_progress)))


@app.command()
def graph(similarities: bool = True) -> None:
    """Recompute embedding neighbours and rebuild the knowledge graph."""
    _setup_logging()
    from .pipeline import graph as g

    async def go():
        out = {}
        if similarities:
            out["similarities"] = await g.compute_similarities()
        out["graph"] = await g.rebuild_graph()
        return out
    _print(_run(go()))


@app.command()
def revalidate(all: Annotated[bool, typer.Option("--all", help="re-run even if already validated by this validator version")] = False,
               concurrency: int = 4) -> None:
    """Re-run validation/extraction for current documents (offline; use after upgrading mlcroissant or the rubric)."""
    _setup_logging()
    from . import db
    from .pipeline import crawl
    from .pipeline import index as idx
    from .pipeline.validate import VALIDATOR_VERSION

    async def go():
        rows = await db.fetch_all(
            """SELECT l.current_document_id AS id FROM links l
               LEFT JOIN validations v ON v.document_id = l.current_document_id
               WHERE l.current_document_id IS NOT NULL
                 AND (%s OR v.validator_version IS DISTINCT FROM %s)""",
            (all, VALIDATOR_VERSION))
        sem = asyncio.Semaphore(concurrency)
        counts: dict[str, int] = {}

        async def one(doc_id: int):
            async with sem:
                res = await crawl.process_document(doc_id)
                if res:
                    counts[res.status] = counts.get(res.status, 0) + 1
        await asyncio.gather(*(one(r["id"]) for r in rows))
        return {"revalidated": len(rows), "statuses": counts, "index": await idx.index_pending()}
    _print(_run(go()))


@app.command()
def submit(url: str) -> None:
    """Add one Croissant URL (or HTML page embedding Croissant) and process it immediately."""
    _setup_logging()
    from .pipeline import crawl, store
    from .pipeline import index as idx

    async def go():
        await store.ensure_sources()
        link_id = await crawl.submit_url(url)
        res = await crawl.crawl_link(link_id, force=True)
        if res.get("process"):
            await idx.index_links([link_id])
        return {"link_id": link_id, **res}
    _print(_run(go()))


@app.command()
def validate(target: str) -> None:
    """Validate a local Croissant file or URL and print the report (no database needed)."""
    from .pipeline import jsonld
    from .pipeline.validate import validate as run_validate

    if target.startswith(("http://", "https://")):
        from .http import PoliteClient

        async def fetch():
            async with PoliteClient() as c:
                return await c.get(target)
        res = asyncio.run(fetch())
        if not res.ok:
            typer.echo(f"HTTP {res.status} {res.error or ''}", err=True)
            raise typer.Exit(2)
        doc, err = jsonld.parse_document(res.content, res.headers.get("content-type", ""))
    else:
        doc, err = jsonld.parse_document(Path(target).read_bytes(), "application/json")
    if doc is None:
        typer.echo(f"cannot parse: {err}", err=True)
        raise typer.Exit(2)
    report, rec = run_validate(doc)
    _print({
        "status": report.status, "croissant_version": report.croissant_version,
        "quality_score": report.quality_score, "quality_tier": report.quality_tier,
        "errors": report.errors, "warnings": report.warnings,
        "categories": report.quality_breakdown.get("categories"),
        "dataset": rec.to_json() if rec else None,
    })
    raise typer.Exit(0 if report.is_valid else 1)


@app.command()
def stats() -> None:
    """Print crawl/index statistics."""
    from .web.queries import overview_stats

    _print(_run(overview_stats()))


@app.command()
def export(out: Path = Path("exports/croissant.jsonl.gz"), valid_only: bool = True) -> None:
    """Export the Croissant store (current version of every dataset) as JSON Lines."""
    from . import db

    async def go():
        out.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if out.suffix == ".gz" else open
        n = 0
        where = "WHERE d.validation_status IN ('valid','valid_with_warnings')" if valid_only else ""
        async with db.connection() as conn, conn.transaction():  # named cursors need a transaction
            async with conn.cursor(name="export") as cur:
                await cur.execute(
                    f"""SELECT d.link_id, d.source_id, l.external_id, l.croissant_url, doc.sha256, doc.fetched_at,
                               d.quality_score, d.validation_status, doc.content
                        FROM datasets d JOIN links l ON l.id = d.link_id JOIN documents doc ON doc.id = d.document_id
                        {where} ORDER BY d.link_id""")
                with opener(out, "wb") as fh:
                    async for row in cur:
                        fh.write(orjson.dumps(row, default=str) + b"\n")
                        n += 1
        return {"exported": n, "path": str(out)}
    _print(_run(go()))


@app.command()
def worker() -> None:
    """Run the Temporal worker (all task queues)."""
    _setup_logging()
    from .orchestration.worker import main

    asyncio.run(main())


@app.command()
def web(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Serve the web UI + JSON API."""
    import uvicorn

    _setup_logging()
    uvicorn.run("croissant_crawler.web.app:app", host=host, port=port, reload=reload, log_level="info")


@wf.command("start")
def wf_start(
    sources: Annotated[list[str] | None, typer.Argument()] = None,
    limit: int = 1000,
    enrich: bool = True,
    graph: bool = True,
    discover: bool = True,
    wait: bool = False,
) -> None:
    """Start the PipelineWorkflow on Temporal."""
    from .orchestration.client import start_pipeline

    srcs = sources or get_settings().sources_enabled
    _print(asyncio.run(start_pipeline(srcs, limit, enrich=enrich, graph=graph, discover=discover, wait=wait)))


@wf.command("schedule")
def wf_schedule(cron: str = "0 3 * * *", limit: int = 5000, delete: bool = False) -> None:
    """Create (or delete) a recurring pipeline schedule (default: nightly at 03:00 UTC)."""
    from .orchestration.client import upsert_schedule

    _print(asyncio.run(upsert_schedule(cron, limit, delete=delete)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
