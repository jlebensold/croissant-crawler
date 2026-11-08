"""Build search documents from the store and push them to Typesense."""

from __future__ import annotations

import logging
import math
from typing import Any

from .. import db
from ..search import Typesense
from .text import markdown_to_text

log = logging.getLogger(__name__)

DATASET_SELECT = """
SELECT d.*, l.external_id, l.landing_url, l.popularity, l.source_meta, l.croissant_url,
       e.output AS enrichment
FROM datasets d
JOIN links l ON l.id = d.link_id
LEFT JOIN LATERAL (
    SELECT output FROM enrichments en
    WHERE en.link_id = d.link_id
    ORDER BY en.created_at DESC LIMIT 1
) e ON TRUE
"""


def popularity_norm(pop: float) -> float:
    return min(1.0, math.log10(1 + max(pop or 0, 0)) / 7.0)  # 10M downloads ~ 1.0


def rank_score(quality: float, popularity: float, placeholder: bool) -> float:
    r = 0.6 * (quality / 100.0) + 0.4 * popularity_norm(popularity)
    return round(r - (0.35 if placeholder else 0.0), 4)


def _uniq(values) -> list[str]:
    seen, out = set(), []
    for v in values:
        if v and str(v).lower() not in seen:
            seen.add(str(v).lower())
            out.append(str(v))
    return out


def build_doc(row: dict[str, Any]) -> dict[str, Any]:
    extra = row.get("extra") or {}
    enr = row.get("enrichment") or {}
    placeholder = bool(enr.get("is_test_or_placeholder", False))
    langs = _uniq((enr.get("languages") or []) + [lang for lang in extra.get("in_language", []) if len(lang) <= 3])
    doc = {
        "id": str(row["link_id"]),
        "name": row["name"],
        "alternate_names": extra.get("alternate_names", [])[:5],
        "description": markdown_to_text(row["description"] or "")[:8000],
        "keywords": row["keywords"][:60],
        "creators": _uniq(c.get("name") for c in (row["creators"] or []))[:20],
        "organizations": _uniq(enr.get("organizations") or [])[:20],
        "source": row["source_id"],
        "external_id": row["external_id"],
        "license": row["license"] or "Unknown",
        "tasks": _uniq((enr.get("tasks") or []) + extra.get("tasks", [])),
        "modalities": _uniq((enr.get("modalities") or []) + extra.get("modalities", [])),
        "domains": _uniq(enr.get("domains") or []),
        "languages": langs,
        "formats": _uniq(_short_format(f) for f in row["encoding_formats"] or []),
        "croissant_version": row["croissant_version"] or "unknown",
        "validation_status": row["validation_status"],
        "quality_tier": row["quality_tier"],
        "quality_score": float(row["quality_score"]),
        "popularity": float(row["popularity"] or 0),
        "rank_score": rank_score(float(row["quality_score"]), float(row["popularity"] or 0), placeholder),
        "is_placeholder": placeholder,
        "enriched": bool(enr),
        "updated_at": int(row["updated_at"].timestamp()),
        "num_fields": int(row["num_fields"]),
        "num_files": int(row["num_files"]),
        "url": row["landing_url"] or row["url"] or "",
    }
    if enr.get("summary"):
        doc["summary"] = enr["summary"]
    if row.get("date_published"):
        doc["date_published"] = int(row["date_published"].timestamp())
    return doc


def _short_format(fmt: str) -> str:
    f = fmt.lower()
    for key, label in (("parquet", "parquet"), ("csv", "csv"), ("json", "json"), ("zip", "zip"),
                       ("tab-separated", "tsv"), ("excel", "excel"), ("arff", "arff"), ("git", "git"),
                       ("image", "image"), ("audio", "audio"), ("video", "video"), ("text/plain", "text"),
                       ("tar", "tar"), ("hdf", "hdf5"), ("netcdf", "netcdf"), ("pdf", "pdf")):
        if key in f:
            return label
    return f.split("/")[-1][:30]


async def index_rows(rows: list[dict], ts: Typesense) -> dict[str, Any]:
    docs = [build_doc(r) for r in rows]
    ok, errors = await ts.upsert(docs)
    if ok:
        good = [int(d["id"]) for d in docs]
        await db.execute("UPDATE datasets SET indexed_at = now() WHERE link_id = ANY(%s)", (good,))
    if errors:
        log.warning("index errors: %s", errors[:3])
    return {"indexed": ok, "errors": errors[:10]}


async def index_links(link_ids: list[int]) -> dict[str, Any]:
    if not link_ids:
        return {"indexed": 0, "errors": []}
    rows = await db.fetch_all(DATASET_SELECT + " WHERE d.link_id = ANY(%s)", (link_ids,))
    async with Typesense() as ts:
        await ts.ensure_collection()
        return await index_rows(rows, ts)


async def index_pending(batch_size: int = 200, max_batches: int = 1000) -> dict[str, int]:
    total = 0
    async with Typesense() as ts:
        await ts.ensure_collection()
        for _ in range(max_batches):
            rows = await db.fetch_all(
                DATASET_SELECT + " WHERE d.indexed_at IS NULL OR d.indexed_at < d.updated_at "
                "ORDER BY d.link_id LIMIT %s", (batch_size,)
            )
            if not rows:
                break
            res = await index_rows(rows, ts)
            total += res["indexed"]
            if res["indexed"] == 0:
                break
    return {"indexed": total}


async def reindex_all() -> dict[str, int]:
    async with Typesense() as ts:
        await ts.ensure_collection(recreate=True)
    await db.execute("UPDATE datasets SET indexed_at = NULL")
    return await index_pending()
