"""Crawl services: discover -> fetch -> validate/extract -> store.

These coroutines are the unit of work for both orchestrators (Temporal activities and the
local asyncio runner). They are idempotent: re-running any of them converges to the same state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any

from .. import db
from ..config import get_settings
from ..http import PoliteClient
from ..sources import get_source
from ..sources.web import WebSource
from . import jsonld as J
from . import store
from .validate import validate

log = logging.getLogger(__name__)

_clients: dict[int, PoliteClient] = {}


def client() -> PoliteClient:
    """Process-wide polite client (one per event loop) so rate limits are shared."""
    key = id(asyncio.get_running_loop())
    c = _clients.get(key)
    if c is None:
        c = _clients[key] = PoliteClient()
    return c


# --------------------------------------------------------------------------- discovery


@dataclass
class DiscoverPageResult:
    source_id: str
    links_seen: int
    links_new: int
    next_cursor: dict[str, Any] | None


async def discover_page(source_id: str, cursor: dict[str, Any] | None) -> DiscoverPageResult:
    src = get_source(source_id)
    if cursor is None:
        await store.set_cursor(source_id, None, started=True)
    page = await src.discover_page(client(), cursor)
    new = await store.upsert_links(source_id, page.links)
    await store.set_cursor(source_id, page.next_cursor, finished=page.next_cursor is None)
    if page.links:
        await store.log_event("discovered", source_id=source_id, seen=len(page.links), new=new)
    return DiscoverPageResult(source_id, len(page.links), new, page.next_cursor)


async def discover(source_id: str, max_links: int | None = None, resume: bool = False) -> dict[str, int]:
    cursor = await store.get_cursor(source_id) if resume else None
    seen = new = pages = 0
    while True:
        res = await discover_page(source_id, cursor)
        seen += res.links_seen
        new += res.links_new
        pages += 1
        cursor = res.next_cursor
        if cursor is None or (max_links is not None and seen >= max_links):
            break
    return {"pages": pages, "seen": seen, "new": new}


# --------------------------------------------------------------------------- fetch


@dataclass
class FetchOutcome:
    link_id: int
    status: str  # ok | not_modified | skipped | deferred | not_found | forbidden | error | invalid
    changed: bool = False
    document_id: int | None = None
    http_status: int | None = None
    error: str | None = None
    needs_processing: bool = False  # validate/extract required (changed doc or never processed)

    def to_dict(self) -> dict:
        return asdict(self)


async def fetch_link(link_id: int, force: bool = False) -> FetchOutcome:
    s = get_settings()
    link = await store.get_link(link_id)
    if link is None:
        return FetchOutcome(link_id, "error", error="link not found")
    min_h, max_h = s.recrawl_min_hours, s.recrawl_max_days * 24
    prev_h = float(link["recrawl_hours"] or min_h)

    # Upstream revision unchanged since our last successful fetch -> no request needed.
    if (
        not force
        and link["current_document_id"]
        and link["source_version"]
        and link["source_version"] == link["fetched_source_version"]
        and link["status"] in ("ok", "not_modified")
    ):
        hrs = store.next_interval_hours(prev_h, "skipped", 0, min_h, max_h)
        await store.record_fetch(link_id, status=link["status"], outcome="skipped", http_status=None,
                                 recrawl_hours=hrs, count_fetch=False)
        doc_id = link["current_document_id"]
        return FetchOutcome(link_id, "skipped", document_id=doc_id,
                            needs_processing=not await _is_processed(doc_id))

    res = await client().get(
        link["croissant_url"],
        etag=None if force else link["etag"],
        last_modified=None if force else link["last_modified"],
    )
    errors = int(link["consecutive_errors"] or 0)
    common = {"http_status": res.status or None, "final_url": res.final_url}

    if res.status == 0 and (res.error or "").startswith("circuit open"):
        # The whole upstream is unhealthy: defer without penalising this link.
        await store.defer_link(link_id, minutes=15)
        return FetchOutcome(link_id, "deferred", error=res.error)

    if res.not_modified:
        hrs = store.next_interval_hours(prev_h, "not_modified", 0, min_h, max_h)
        await store.record_fetch(link_id, status="ok", outcome="not_modified", recrawl_hours=hrs,
                                 fetched_source_version=link["source_version"], **common)
        doc_id = link["current_document_id"]
        return FetchOutcome(link_id, "not_modified", document_id=doc_id, http_status=304,
                            needs_processing=bool(doc_id) and not await _is_processed(doc_id))

    if not res.ok:
        if res.status in (404, 410):
            status, outcome = ("gone" if errors + 1 >= 3 else "not_found"), "not_found"
        elif res.status in (401, 403):
            status = outcome = "forbidden"
        else:
            status = outcome = "error"
        err = res.error or f"HTTP {res.status}"
        hrs = store.next_interval_hours(prev_h, outcome, errors + 1, min_h, max_h)
        await store.record_fetch(link_id, status=status, outcome=outcome, recrawl_hours=hrs, error=err, **common)
        await store.log_event("error", source_id=link["source_id"], link_id=link_id, http_status=res.status, error=err[:300])
        return FetchOutcome(link_id, status, http_status=res.status, error=err)

    doc, perr = J.parse_document(res.content, res.headers.get("content-type", ""))
    if doc is None:
        hrs = store.next_interval_hours(prev_h, "invalid", errors + 1, min_h, max_h)
        await store.record_fetch(link_id, status="invalid", outcome="invalid", recrawl_hours=hrs, error=perr, **common)
        await store.log_event("error", source_id=link["source_id"], link_id=link_id, http_status=res.status, error=perr)
        return FetchOutcome(link_id, "invalid", http_status=res.status, error=perr)

    sha = J.sha256_of(doc)
    document_id, is_new = await store.insert_document(link_id, sha, len(res.content), doc)
    changed = is_new or link["current_document_id"] != document_id
    hrs = store.next_interval_hours(prev_h, "changed" if changed else "unchanged", 0, min_h, max_h)
    await store.record_fetch(
        link_id, status="ok", outcome="changed" if changed else "unchanged", recrawl_hours=hrs,
        etag=res.headers.get("etag"), last_modified=res.headers.get("last-modified"),
        document_id=document_id, changed=changed, fetched_source_version=link["source_version"], **common,
    )
    await store.log_event("changed" if changed else "unchanged", source_id=link["source_id"], link_id=link_id, sha256=sha[:12])
    return FetchOutcome(link_id, "ok", changed=changed, document_id=document_id, http_status=res.status,
                        needs_processing=changed or not await _is_processed(document_id))


# --------------------------------------------------------------------------- validate + extract


@dataclass
class ProcessOutcome:
    document_id: int
    link_id: int
    status: str
    quality_score: float
    quality_tier: str

    def to_dict(self) -> dict:
        return asdict(self)


def _validate_sync(content: dict):
    return validate(content)


async def process_document(document_id: int) -> ProcessOutcome | None:
    """Validate a stored document and refresh the dataset row (only if it is the link's current doc)."""
    doc = await store.get_document(document_id)
    if doc is None:
        return None
    report, rec = await asyncio.to_thread(_validate_sync, doc["content"])
    await store.save_validation(document_id, report)
    link = await store.get_link(doc["link_id"])
    if rec is not None and link and link["current_document_id"] == document_id:
        await store.upsert_dataset(doc["link_id"], document_id, doc["source_id"], rec, report)
    elif rec is None:
        # Not Croissant at all: drop any stale dataset row and flag the link.
        await db.execute("DELETE FROM datasets WHERE link_id = %s", (doc["link_id"],))
        await db.execute("UPDATE links SET status = 'invalid', last_error = %s WHERE id = %s",
                         ((report.errors or ["not croissant"])[0], doc["link_id"]))
    await store.log_event("validated", source_id=doc["source_id"], link_id=doc["link_id"],
                          status=report.status, score=report.quality_score)
    return ProcessOutcome(document_id, doc["link_id"], report.status, report.quality_score, report.quality_tier)


async def submit_url(url: str) -> int:
    """Register an arbitrary Croissant URL (user submission). Reuses an existing link if known."""
    row = await db.fetch_one("SELECT id FROM links WHERE croissant_url = %s", (url,))
    if row:
        await db.execute("UPDATE links SET next_fetch_at = now() WHERE id = %s", (row["id"],))
        return int(row["id"])
    return await store.add_link("web", WebSource().link_for(url))


async def crawl_link(link_id: int, force: bool = False) -> dict:
    """Fetch + process a single link (used for submissions and the local runner)."""
    out = await fetch_link(link_id, force=force)
    result: dict[str, Any] = {"fetch": out.to_dict()}
    if out.document_id and (out.needs_processing or force):
        p = await process_document(out.document_id)
        result["process"] = p.to_dict() if p else None
    return result


async def _is_processed(document_id: int) -> bool:
    """True when the document has a validation from the current validator version."""
    from .validate import VALIDATOR_VERSION

    return bool(await db.fetch_one(
        "SELECT 1 FROM validations WHERE document_id = %s AND validator_version = %s",
        (document_id, VALIDATOR_VERSION),
    ))
