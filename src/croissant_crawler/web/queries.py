"""Read-side SQL used by the web UI / JSON API."""

from __future__ import annotations

from typing import Any

from .. import db
from ..pipeline import jsonld as J


async def overview_stats() -> dict[str, Any]:
    totals = await db.fetch_one(
        """SELECT
             (SELECT count(*) FROM links) AS links,
             (SELECT count(*) FROM links WHERE status = 'pending') AS pending,
             (SELECT count(*) FROM documents) AS documents,
             (SELECT count(*) FROM datasets) AS datasets,
             (SELECT count(*) FROM datasets WHERE validation_status IN ('valid','valid_with_warnings')) AS valid,
             (SELECT count(*) FROM datasets WHERE indexed_at IS NOT NULL) AS indexed,
             (SELECT count(DISTINCT link_id) FROM enrichments) AS enriched,
             (SELECT coalesce(sum(cost_usd),0) FROM enrichments) AS llm_cost_usd,
             (SELECT count(*) FROM graph_nodes) AS graph_nodes,
             (SELECT count(*) FROM graph_edges) AS graph_edges,
             (SELECT round(avg(quality_score)::numeric, 1) FROM datasets) AS avg_quality,
             (SELECT max(at) FROM crawl_events) AS last_activity"""
    )
    per_source = await db.fetch_all(
        """SELECT s.id, s.name, s.last_discovery_started_at, s.last_discovery_finished_at,
                  count(l.id) AS links,
                  count(l.id) FILTER (WHERE l.status = 'ok') AS ok,
                  count(l.id) FILTER (WHERE l.status = 'pending') AS pending,
                  count(l.id) FILTER (WHERE l.status IN ('error')) AS errors,
                  count(l.id) FILTER (WHERE l.status IN ('not_found','gone')) AS not_found,
                  count(l.id) FILTER (WHERE l.status IN ('forbidden')) AS forbidden,
                  count(l.id) FILTER (WHERE l.status IN ('invalid')) AS invalid,
                  max(l.last_fetched_at) AS last_fetch,
                  (SELECT count(*) FROM datasets d WHERE d.source_id = s.id) AS datasets,
                  (SELECT count(*) FROM datasets d WHERE d.source_id = s.id
                      AND d.validation_status IN ('valid','valid_with_warnings')) AS valid,
                  (SELECT round(avg(d.quality_score)::numeric,1) FROM datasets d WHERE d.source_id = s.id) AS avg_quality
           FROM sources s LEFT JOIN links l ON l.source_id = s.id
           GROUP BY s.id ORDER BY count(l.id) DESC"""
    )
    tiers = await db.fetch_all(
        "SELECT source_id, quality_tier, count(*) AS n FROM datasets GROUP BY 1, 2 ORDER BY 1, 2"
    )
    statuses = await db.fetch_all(
        "SELECT validation_status, croissant_version, count(*) AS n FROM datasets GROUP BY 1, 2 ORDER BY 3 DESC"
    )
    top_errors = await db.fetch_all(
        """SELECT err, count(*) AS n FROM (
             SELECT regexp_replace(jsonb_array_elements_text(v.errors), '\\[[^\\]]*\\]|''[^'']*''|"[^"]*"', '…', 'g') AS err
             FROM datasets d JOIN validations v ON v.document_id = d.document_id) x
           GROUP BY err ORDER BY n DESC LIMIT 8"""
    )
    return {"totals": totals, "sources": per_source, "tiers": tiers, "statuses": statuses, "top_errors": top_errors}


async def recent_events(limit: int = 30, kinds: list[str] | None = None) -> list[dict]:
    return await db.fetch_all(
        """SELECT e.at, e.source_id, e.link_id, e.kind, e.detail,
                  coalesce(d.name, split_part(l.external_id, '|', 2), l.external_id) AS name,
                  d.link_id IS NOT NULL AS has_dataset
           FROM crawl_events e
           LEFT JOIN datasets d ON d.link_id = e.link_id
           LEFT JOIN links l ON l.id = e.link_id
           WHERE (%(kinds)s::text[] IS NULL OR e.kind = ANY(%(kinds)s))
           ORDER BY e.id DESC LIMIT %(limit)s""",
        {"kinds": kinds, "limit": limit},
    )


async def recent_errors(limit: int = 15) -> list[dict]:
    return await db.fetch_all(
        """SELECT id, source_id, external_id, croissant_url, status, http_status, last_error, last_fetched_at,
                  consecutive_errors, next_fetch_at
           FROM links WHERE status IN ('error','not_found','forbidden','invalid','gone')
           ORDER BY last_fetched_at DESC NULLS LAST LIMIT %s""",
        (limit,),
    )


async def dataset_detail(link_id: int) -> dict[str, Any] | None:
    row = await db.fetch_one(
        """SELECT d.*, l.external_id, l.croissant_url, l.landing_url, l.final_url, l.source_meta, l.popularity,
                  l.status AS link_status, l.discovered_at, l.last_seen_at, l.last_fetched_at, l.last_changed_at,
                  l.next_fetch_at, l.fetch_count, l.recrawl_hours, l.http_status, l.last_error,
                  doc.sha256, doc.fetched_at, doc.size_bytes, doc.content,
                  v.status AS v_status, v.errors, v.warnings, v.quality_breakdown, v.validator_version,
                  v.duration_ms, v.validated_at
           FROM datasets d
           JOIN links l ON l.id = d.link_id
           JOIN documents doc ON doc.id = d.document_id
           LEFT JOIN validations v ON v.document_id = d.document_id
           WHERE d.link_id = %s""",
        (link_id,),
    )
    if row is None:
        return None
    enr = await db.fetch_one(
        "SELECT output, model, created_at, cost_usd, sha256 FROM enrichments WHERE link_id = %s ORDER BY created_at DESC LIMIT 1",
        (link_id,),
    )
    versions = await db.fetch_all(
        """SELECT doc.id, doc.sha256, doc.fetched_at, doc.size_bytes, v.status, v.quality_score
           FROM documents doc LEFT JOIN validations v ON v.document_id = doc.id
           WHERE doc.link_id = %s ORDER BY doc.fetched_at DESC""",
        (link_id,),
    )
    content = row.pop("content")
    row["record_sets"] = summarize_record_sets(content)
    row["files"] = summarize_files(content)
    row["enrichment"] = enr
    row["versions"] = versions
    row["related"] = await related(link_id)
    return row


def summarize_record_sets(doc: dict, max_sets: int = 12, max_fields: int = 25) -> list[dict]:
    out = []
    for rs in J.as_list(J.get(doc, "recordSet"))[:max_sets]:
        if not isinstance(rs, dict):
            continue
        fields = []
        for f in J.as_list(J.get(rs, "field"))[:max_fields]:
            if isinstance(f, dict):
                fields.append({
                    "name": J.text(J.get(f, "name")) or J.text(f.get("@id")) or "?",
                    "type": ", ".join(t.split(":")[-1] for t in J.texts(J.get(f, "dataType"))),
                    "description": (J.text(J.get(f, "description")) or "")[:200],
                })
        out.append({
            "name": J.text(J.get(rs, "name")) or J.text(rs.get("@id")) or "record set",
            "description": (J.text(J.get(rs, "description")) or "")[:300],
            "n_fields": len(J.as_list(J.get(rs, "field"))),
            "fields": fields,
        })
    return out


def summarize_files(doc: dict, limit: int = 15) -> list[dict]:
    out = []
    for item in J.as_list(J.get(doc, "distribution"))[:limit]:
        if not isinstance(item, dict):
            continue
        out.append({
            "type": (J.types(item) or ["?"])[0],
            "name": J.text(J.get(item, "name")) or J.text(item.get("@id")) or "",
            "format": J.text(J.get(item, "encodingFormat")) or "",
            "size": J.text(J.get(item, "contentSize")) or "",
            "url": J.text(J.get(item, "contentUrl")) or "",
            "includes": J.text(J.get(item, "includes")) or "",
            "checksum": bool(J.get(item, "sha256") or J.get(item, "md5")),
        })
    return out


async def related(link_id: int, limit: int = 12) -> dict[str, list[dict]]:
    nid = f"dataset:{link_id}"
    rows = await db.fetch_all(
        """SELECT e.type, e.weight, n.id, n.type AS ntype, n.label, n.props,
                  CASE WHEN e.src = %(nid)s THEN 'out' ELSE 'in' END AS direction
           FROM graph_edges e JOIN graph_nodes n ON n.id = CASE WHEN e.src = %(nid)s THEN e.dst ELSE e.src END
           WHERE (e.src = %(nid)s OR e.dst = %(nid)s)
             AND e.type IN ('SAME_AS','SIMILAR_TO','DERIVED_FROM','CITES','CREATED_BY')
           ORDER BY e.weight DESC LIMIT 200""",
        {"nid": nid},
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        key = r["type"] if r["type"] != "DERIVED_FROM" else ("DERIVED_FROM" if r["direction"] == "out" else "DERIVATIVES")
        bucket = out.setdefault(key, [])
        if len(bucket) < limit:
            bucket.append(r)
    return out


async def document(doc_id: int) -> dict | None:
    return await db.fetch_one("SELECT id, link_id, sha256, fetched_at, content FROM documents WHERE id = %s", (doc_id,))


# ------------------------------------------------------------------ graph


async def graph_overview(per_type: int = 14) -> dict[str, Any]:
    """Hub nodes (tasks, modalities, domains, licenses, orgs, sources) + co-occurrence edges."""
    hubs = await db.fetch_all(
        """WITH deg AS (
             SELECT n.id, n.type, n.label, count(*) AS degree
             FROM graph_nodes n JOIN graph_edges e ON e.dst = n.id
             WHERE n.type IN ('task','modality','domain','license','organization','source')
             GROUP BY n.id),
           ranked AS (SELECT *, row_number() OVER (PARTITION BY type ORDER BY degree DESC) AS rk FROM deg)
           SELECT id, type, label, degree FROM ranked WHERE rk <= %s""",
        (per_type,),
    )
    ids = [h["id"] for h in hubs]
    edges = await db.fetch_all(
        """SELECT a.dst AS src, b.dst AS dst, count(*) AS weight
           FROM graph_edges a JOIN graph_edges b ON a.src = b.src AND a.dst < b.dst
           WHERE a.dst = ANY(%(ids)s) AND b.dst = ANY(%(ids)s) AND a.src LIKE 'dataset:%%'
           GROUP BY a.dst, b.dst HAVING count(*) >= 2
           ORDER BY weight DESC LIMIT 400""",
        {"ids": ids},
    )
    return {"nodes": hubs, "edges": edges}


async def graph_neighborhood(node_id: str, limit: int = 60) -> dict[str, Any]:
    center = await db.fetch_one("SELECT id, type, label, props FROM graph_nodes WHERE id = %s", (node_id,))
    if center is None:
        return {"nodes": [], "edges": []}
    rows = await db.fetch_all(
        """SELECT e.src, e.dst, e.type, e.weight, n.id, n.type AS ntype, n.label, n.props
           FROM graph_edges e
           JOIN graph_nodes n ON n.id = CASE WHEN e.src = %(id)s THEN e.dst ELSE e.src END
           WHERE e.src = %(id)s OR e.dst = %(id)s
           ORDER BY (n.props->>'quality')::float DESC NULLS LAST, e.weight DESC
           LIMIT %(limit)s""",
        {"id": node_id, "limit": limit},
    )
    degree = await db.fetch_one(
        "SELECT count(*) AS n FROM graph_edges WHERE src = %s OR dst = %s", (node_id, node_id)
    )
    nodes = {center["id"]: {"id": center["id"], "type": center["type"], "label": center["label"], "props": center["props"]}}
    edges = []
    for r in rows:
        nodes.setdefault(r["id"], {"id": r["id"], "type": r["ntype"], "label": r["label"], "props": r["props"]})
        edges.append({"src": r["src"], "dst": r["dst"], "type": r["type"], "weight": r["weight"]})
    return {"center": node_id, "degree": degree["n"] if degree else 0, "nodes": list(nodes.values()), "edges": edges}


async def graph_search(q: str, limit: int = 15) -> list[dict]:
    return await db.fetch_all(
        """SELECT n.id, n.type, n.label,
                  (SELECT count(*) FROM graph_edges e WHERE e.src = n.id OR e.dst = n.id) AS degree
           FROM graph_nodes n WHERE n.label ILIKE %s
           ORDER BY (n.type <> 'dataset') DESC, length(n.label) LIMIT %s""",
        (f"%{q}%", limit),
    )


async def graph_stats() -> dict[str, Any]:
    nodes = await db.fetch_all("SELECT type, count(*) AS n FROM graph_nodes GROUP BY type ORDER BY n DESC")
    edges = await db.fetch_all("SELECT type, count(*) AS n FROM graph_edges GROUP BY type ORDER BY n DESC")
    return {"nodes": nodes, "edges": edges}


async def sources() -> list[dict]:
    return await db.fetch_all("SELECT * FROM sources ORDER BY id")
