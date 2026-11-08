"""FastAPI application: lightweight server-rendered UI (Jinja2 + htmx) and a JSON API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, ORJSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import __version__, db
from ..config import get_settings
from ..pipeline import jsonld
from ..pipeline.validate import validate as run_validate
from ..search import Typesense
from ..sources import all_source_ids
from . import queries
from .search_service import FACET_LABELS, SORTS, SearchRequest, run_search, similar

log = logging.getLogger(__name__)
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await db.migrate()
    except Exception as exc:  # the UI still renders an error banner if the DB is down
        log.error("migration failed: %s", exc)
    yield
    await db.close_pool()


app = FastAPI(title="Croissant Crawler", version=__version__, lifespan=lifespan,
              default_response_class=ORJSONResponse,
              description="Search and inspect an index of MLCommons Croissant dataset metadata.")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ------------------------------------------------------------------ template helpers

def _ago(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, UTC)
    delta = datetime.now(UTC) - value
    s = delta.total_seconds()
    future = s < 0
    s = abs(s)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            out = f"{int(s // size)}{unit}"
            return f"in {out}" if future else f"{out} ago"
    return "just now"


def _num(value: Any) -> str:
    """Exact counts with separators below 100k; compact (1.2M) above."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e5)):
        if abs(v) >= size:
            div = 1e3 if unit == "k" else size
            return f"{v / div:.1f}".rstrip("0").rstrip(".") + unit
    return f"{int(v):,}" if v == int(v) else f"{v:,.1f}"


def _bytes(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not v:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1000:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1000
    return f"{v:.1f} PB"


def _date(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, UTC)
    return value.strftime("%Y-%m-%d")


templates.env.filters.update(ago=_ago, num=_num, bytes=_bytes, date=_date,
                             tojson_pretty=lambda v: orjson.dumps(v, option=orjson.OPT_INDENT_2, default=str).decode())
templates.env.globals.update(settings=get_settings(), version=__version__, facet_labels=FACET_LABELS,
                             sorts=list(SORTS), all_sources=all_source_ids())


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------------ HTML pages

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request):
    req = SearchRequest.from_query(request.query_params)
    error, results = None, None
    try:
        results = await run_search(req)
    except Exception as exc:
        error = f"Search is unavailable: {exc}"
    stats = None
    try:
        stats = (await queries.overview_stats())["totals"]
    except Exception:
        pass
    template = "_results.html" if request.headers.get("HX-Request") else "search.html"
    return render(request, template, req=req, results=results, error=error, stats=stats)


@app.get("/datasets/{link_id}", response_class=HTMLResponse, include_in_schema=False)
async def dataset_page(request: Request, link_id: int):
    ds = await queries.dataset_detail(link_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    sims = await similar(link_id)
    return render(request, "dataset.html", ds=ds, similar=sims)


@app.get("/graph", response_class=HTMLResponse, include_in_schema=False)
async def graph_page(request: Request, focus: str | None = None):
    return render(request, "graph.html", focus=focus, gstats=await queries.graph_stats())


@app.get("/crawl", response_class=HTMLResponse, include_in_schema=False)
async def crawl_page(request: Request):
    runs, runs_error = [], None
    try:
        from ..orchestration.client import recent_runs
        runs = await recent_runs(8)
    except Exception as exc:
        runs_error = str(exc)[:200]
    return render(request, "crawl.html", stats=await queries.overview_stats(), runs=runs,
                  runs_error=runs_error, errors=await queries.recent_errors())


@app.get("/crawl/events", response_class=HTMLResponse, include_in_schema=False)
async def crawl_events_fragment(request: Request):
    return render(request, "_events.html", events=await queries.recent_events(25))


@app.get("/validate", response_class=HTMLResponse, include_in_schema=False)
async def validate_page(request: Request):
    return render(request, "validate.html", report=None)


@app.post("/validate", response_class=HTMLResponse, include_in_schema=False)
async def validate_submit(request: Request):
    form = await request.form()
    url = (form.get("url") or "").strip()
    text = (form.get("document") or "").strip()
    report = await _validate_input(url or None, text or None)
    return render(request, "validate.html", report=report, url=url, document=text)


@app.get("/about", response_class=HTMLResponse, include_in_schema=False)
async def about_page(request: Request):
    return render(request, "about.html")


# ------------------------------------------------------------------ JSON API

@app.get("/healthz", tags=["meta"])
async def healthz():
    out: dict[str, Any] = {"version": __version__}
    try:
        await db.fetch_one("SELECT 1")
        out["postgres"] = "ok"
    except Exception as exc:
        out["postgres"] = f"error: {exc}"
    async with Typesense() as ts:
        out["typesense"] = "ok" if await ts.health() else "unreachable"
    return out


@app.get("/api/search", tags=["search"])
async def api_search(request: Request):
    """Full-text + semantic search. Facet filters are repeated query params, e.g.
    `?q=speech&modalities=audio&source=huggingface&sort=quality`."""
    return await run_search(SearchRequest.from_query(request.query_params))


@app.get("/api/datasets/{link_id}", tags=["datasets"])
async def api_dataset(link_id: int):
    ds = await queries.dataset_detail(link_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    return ds


@app.get("/api/datasets/{link_id}/croissant", tags=["datasets"])
async def api_dataset_croissant(link_id: int):
    """The current stored Croissant JSON-LD for a dataset."""
    row = await db.fetch_one(
        "SELECT doc.content FROM datasets d JOIN documents doc ON doc.id = d.document_id WHERE d.link_id = %s", (link_id,))
    if row is None:
        raise HTTPException(404, "dataset not found")
    return Response(orjson.dumps(row["content"], option=orjson.OPT_INDENT_2), media_type="application/ld+json")


@app.get("/api/datasets/{link_id}/similar", tags=["datasets"])
async def api_similar(link_id: int, k: int = 8):
    return await similar(link_id, k)


@app.get("/api/documents/{doc_id}", tags=["datasets"])
async def api_document(doc_id: int):
    """A specific stored version of a Croissant document."""
    row = await queries.document(doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    return Response(orjson.dumps(row["content"], option=orjson.OPT_INDENT_2), media_type="application/ld+json")


@app.get("/api/stats", tags=["crawl"])
async def api_stats():
    return await queries.overview_stats()


@app.get("/api/events", tags=["crawl"])
async def api_events(limit: int = 50):
    return await queries.recent_events(min(limit, 500))


@app.get("/api/sources", tags=["crawl"])
async def api_sources():
    return await queries.sources()


@app.get("/api/graph/overview", tags=["graph"])
async def api_graph_overview(per_type: int = 14):
    return await queries.graph_overview(min(per_type, 50))


@app.get("/api/graph/node", tags=["graph"])
async def api_graph_node(id: str, limit: int = 60):
    return await queries.graph_neighborhood(id, min(limit, 300))


@app.get("/api/graph/search", tags=["graph"])
async def api_graph_search(q: str):
    return await queries.graph_search(q) if len(q) >= 2 else []


@app.get("/api/graph/stats", tags=["graph"])
async def api_graph_stats():
    return await queries.graph_stats()


@app.get("/api/graph/export", tags=["graph"])
async def api_graph_export():
    """The whole knowledge graph as JSON (nodes + edges), e.g. for Gephi / NetworkX / Neo4j import."""
    nodes = await db.fetch_all("SELECT id, type, label, props FROM graph_nodes")
    edges = await db.fetch_all("SELECT src, dst, type, weight, props FROM graph_edges")
    return {"nodes": nodes, "edges": edges}


class ValidateBody(BaseModel):
    url: str | None = None
    document: dict | None = None


@app.post("/api/validate", tags=["validate"])
async def api_validate(body: ValidateBody):
    """Validate a Croissant document (inline JSON or URL) without storing it."""
    text = orjson.dumps(body.document).decode() if body.document is not None else None
    return await _validate_input(body.url, text)


class SubmitBody(BaseModel):
    url: str


@app.post("/api/submit", tags=["validate"])
async def api_submit(body: SubmitBody):
    """Add a Croissant URL to the index (fetched, validated and indexed immediately)."""
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    from ..pipeline import crawl, index, store

    await store.ensure_sources()
    link_id = await crawl.submit_url(body.url)
    res = await crawl.crawl_link(link_id, force=True)
    if res.get("process"):
        await index.index_links([link_id])
    return {"link_id": link_id, **res}


class PipelineBody(BaseModel):
    sources: list[str] | None = None
    limit: int = 500
    enrich: bool = True
    graph: bool = True


@app.post("/api/admin/pipeline", tags=["admin"])
async def api_start_pipeline(body: PipelineBody):
    """Start a PipelineWorkflow on Temporal (disabled when ENABLE_ADMIN_ACTIONS=false)."""
    s = get_settings()
    if not s.enable_admin_actions:
        raise HTTPException(403, "admin actions are disabled")
    from ..orchestration.client import start_pipeline

    sources = [x for x in (body.sources or s.sources_enabled) if x in all_source_ids()]
    try:
        return await start_pipeline(sources, max(1, min(body.limit, 100_000)), enrich=body.enrich, graph=body.graph)
    except Exception as exc:
        raise HTTPException(503, f"cannot start workflow (is Temporal running?): {exc}") from exc


@app.post("/crawl/start", response_class=HTMLResponse, include_in_schema=False)
async def crawl_start_form(request: Request):
    form = await request.form()
    body = PipelineBody(sources=form.getlist("sources") or None, limit=int(form.get("limit") or 500),
                        enrich=form.get("enrich") == "on", graph=form.get("graph") == "on")
    try:
        res = await api_start_pipeline(body)
        msg = f'Started <a href="{res["ui"]}" target="_blank" rel="noopener">{res["workflow_id"]}</a>'
        return HTMLResponse(f'<div class="notice ok">{msg}</div>')
    except HTTPException as exc:
        return HTMLResponse(f'<div class="notice err">{exc.detail}</div>')


# ------------------------------------------------------------------ helpers

async def _validate_input(url: str | None, text: str | None) -> dict[str, Any]:
    if url:
        from ..pipeline.crawl import client
        res = await client().get(url)
        if not res.ok:
            return {"ok": False, "error": f"HTTP {res.status} {res.error or ''}".strip(), "url": url}
        doc, err = jsonld.parse_document(res.content, res.headers.get("content-type", ""))
    elif text:
        doc, err = jsonld.parse_document(text.encode(), "application/json")
    else:
        return {"ok": False, "error": "provide a URL or a JSON document"}
    if doc is None:
        return {"ok": False, "error": err, "url": url}
    import asyncio
    report, rec = await asyncio.to_thread(run_validate, doc)
    return {
        "ok": True, "url": url, "status": report.status, "croissant_version": report.croissant_version,
        "quality_score": report.quality_score, "quality_tier": report.quality_tier,
        "errors": report.errors, "warnings": report.warnings, "breakdown": report.quality_breakdown,
        "dataset": rec.to_json() if rec else None, "duration_ms": report.duration_ms,
    }


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api") or request.url.path == "/healthz":
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "detail": exc.detail},
                                      status_code=exc.status_code)
