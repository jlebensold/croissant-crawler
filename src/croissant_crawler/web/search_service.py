"""Search request → Typesense query translation, shared by HTML and JSON endpoints."""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from typing import Any

from ..search import FACETS, Typesense

SORTS = {
    "relevance": None,
    "quality": "quality_score:desc,popularity:desc",
    "popularity": "popularity:desc,quality_score:desc",
    "newest": "date_published:desc,rank_score:desc",
    "updated": "updated_at:desc",
}
FACET_LABELS = {
    "source": "Source", "modalities": "Modality", "tasks": "Task", "domains": "Domain", "license": "License",
    "quality_tier": "Quality tier", "validation_status": "Validation", "croissant_version": "Croissant version",
    "languages": "Language", "formats": "Format",
}
START, END = "\u0001", "\u0002"
RELEVANCE_BUCKETS = 6


@dataclass
class SearchRequest:
    q: str = ""
    page: int = 1
    per_page: int = 20
    sort: str = "relevance"
    semantic: bool = True
    show_placeholders: bool = False
    filters: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_query(cls, params) -> SearchRequest:
        filters = {f: [v for v in params.getlist(f) if v] for f in FACETS}
        return cls(
            q=(params.get("q") or "").strip()[:300],
            page=max(1, int(params.get("page") or 1)),
            per_page=min(100, max(1, int(params.get("per_page") or 20))),
            sort=params.get("sort") if params.get("sort") in SORTS else "relevance",
            semantic=params.get("semantic", "1") not in ("0", "false", "off"),
            show_placeholders=params.get("show_placeholders") in ("1", "true", "on"),
            filters={k: v for k, v in filters.items() if v},
        )


def _quote(v: str) -> str:
    return "`" + v.replace("`", "") + "`"


def build_params(req: SearchRequest, semantic_enabled: bool) -> dict[str, Any]:
    clauses = [f"{f}:=[{','.join(_quote(v) for v in vals)}]" for f, vals in req.filters.items()]
    if not req.show_placeholders:
        clauses.append("is_placeholder:=false")
    q = req.q or "*"
    use_vector = semantic_enabled and req.semantic and q != "*"
    params: dict[str, Any] = {
        "q": q,
        "query_by": "name,alternate_names,keywords,summary,creators,description" + (",embedding" if use_vector else ""),
        "query_by_weights": "6,4,3,3,2,1" + (",1" if use_vector else ""),
        "facet_by": ",".join(FACETS),
        "max_facet_values": 12,
        "page": req.page,
        "per_page": req.per_page,
        "exclude_fields": "embedding",
        "highlight_fields": "name,summary,description",
        "highlight_start_tag": START,
        "highlight_end_tag": END,
        "highlight_affix_num_tokens": 24,
        "snippet_threshold": 40,
        "num_typos": 1,
        "prioritize_exact_match": True,
        "text_match_type": "max_weight",
    }
    if use_vector:
        params["vector_query"] = "embedding:([], alpha: 0.7, distance_threshold: 0.55)"
    if clauses:
        params["filter_by"] = " && ".join(clauses)
    sort = SORTS.get(req.sort)
    if sort:
        params["sort_by"] = sort
    elif q == "*":
        params["sort_by"] = "rank_score:desc"
    else:
        # Relevance first, then quality/popularity *within* relevance buckets (works for the
        # hybrid fusion score too): an invalid file no longer outranks an equally relevant good one.
        params["sort_by"] = f"_text_match(buckets: {RELEVANCE_BUCKETS}):desc,rank_score:desc"
    return params


def _marked(text: str) -> str:
    return html.escape(text).replace(START, "<mark>").replace(END, "</mark>")


def shape_results(raw: dict[str, Any], req: SearchRequest) -> dict[str, Any]:
    hits = []
    for h in raw.get("hits", []):
        doc = h["document"]
        hl = {x["field"]: x for x in h.get("highlights", [])}
        name_hl = hl.get("name", {}).get("snippet")
        snippet = None
        for f in ("summary", "description"):
            if f in hl and hl[f].get("snippet"):
                snippet = _marked(hl[f]["snippet"])
                break
        if snippet is None:
            text = doc.get("summary") or doc.get("description") or ""
            snippet = html.escape(text[:320] + ("…" if len(text) > 320 else ""))
        hits.append({
            **doc,
            "name_html": _marked(name_hl) if name_hl else html.escape(doc["name"]),
            "snippet_html": snippet,
            "vector_distance": h.get("vector_distance"),
            "text_match": h.get("text_match"),
        })
    facets = []
    for fc in raw.get("facet_counts", []):
        name = fc["field_name"]
        selected = set(req.filters.get(name, []))
        counts = [{"value": c["value"], "count": c["count"], "selected": c["value"] in selected} for c in fc["counts"]]
        present = {c["value"] for c in counts}
        counts += [{"value": v, "count": 0, "selected": True} for v in selected - present]
        if counts:
            facets.append({"field": name, "label": FACET_LABELS.get(name, name), "values": counts})
    order = list(FACET_LABELS)
    facets.sort(key=lambda f: order.index(f["field"]) if f["field"] in order else 99)
    found = raw.get("found", 0)
    return {
        "found": found,
        "out_of": raw.get("out_of", 0),
        "page": req.page,
        "pages": max(1, math.ceil(found / req.per_page)),
        "search_time_ms": raw.get("search_time_ms"),
        "hits": hits,
        "facets": facets,
    }


async def run_search(req: SearchRequest) -> dict[str, Any]:
    async with Typesense() as ts:
        params = build_params(req, bool(ts.embedding_model))
        try:
            raw = await ts.search(params)
        except Exception:
            if "vector_query" not in params:
                raise
            # embedding model unavailable (e.g. still downloading): degrade to keyword search
            params.pop("vector_query")
            params["query_by"] = params["query_by"].replace(",embedding", "")
            params["query_by_weights"] = ",".join(params["query_by_weights"].split(",")[:-1])
            raw = await ts.search(params)
    return shape_results(raw, req)


async def similar(link_id: int, k: int = 8) -> list[dict[str, Any]]:
    async with Typesense() as ts:
        if not ts.embedding_model:
            return []
        try:
            raw = await ts.search({
                "q": "*", "vector_query": f"embedding:([], id: {link_id}, k: {k + 1})",
                "exclude_fields": "embedding,description", "per_page": k + 1,
                "filter_by": "is_placeholder:=false",
            })
        except Exception:
            return []
    return [{**h["document"], "score": round(1 - h.get("vector_distance", 1), 3)}
            for h in raw.get("hits", []) if h["document"]["id"] != str(link_id)][:k]
