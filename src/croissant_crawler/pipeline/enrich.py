"""LLM enrichment: summaries, controlled-vocabulary facets and entities for the knowledge graph.

Results are cached per (link, document sha256, prompt version): unchanged documents are never
re-sent to the model, which keeps recurring crawls cheap.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import db
from ..db import jsonb
from ..llm import LLMError, OpenRouter
from .text import markdown_to_text

log = logging.getLogger(__name__)

PROMPT_VERSION = "e2"

TASKS = [
    "text-classification", "token-classification", "question-answering", "summarization", "translation",
    "text-generation", "conversational", "instruction-tuning", "preference-alignment", "reasoning",
    "math", "code-generation", "information-retrieval", "sentence-similarity", "fill-mask",
    "automatic-speech-recognition", "text-to-speech", "audio-classification",
    "image-classification", "object-detection", "image-segmentation", "image-captioning",
    "text-to-image", "visual-question-answering", "document-understanding", "ocr",
    "video-understanding", "video-generation", "depth-estimation", "3d-reconstruction",
    "tabular-classification", "tabular-regression", "time-series-forecasting", "anomaly-detection",
    "clustering", "recommendation", "graph-learning", "reinforcement-learning", "robotics",
    "benchmark-evaluation", "other",
]
MODALITIES = ["text", "image", "audio", "video", "tabular", "time-series", "graph", "geospatial",
              "3d", "code", "document", "multimodal", "other"]
DOMAINS = [
    "general", "healthcare-medicine", "biology-life-sciences", "chemistry-materials", "physics-astronomy",
    "earth-climate-environment", "finance-economics", "social-sciences", "education", "legal",
    "government-public-sector", "commerce-retail", "software-engineering", "mathematics",
    "robotics-autonomy", "transportation-mobility", "agriculture-food", "energy", "sports-games",
    "media-entertainment", "linguistics-nlp", "cybersecurity", "manufacturing-industry",
    "ai-safety-alignment", "other",
]
FLAGS = ["missing_description", "boilerplate_description", "unclear_license", "no_schema",
         "synthetic_data", "personal_data_risk", "machine_translated", "benchmark", "mirror_or_copy",
         "tiny_or_toy"]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "tasks", "modalities", "domains", "languages", "organizations",
                 "derived_from", "papers", "quality_flags", "is_test_or_placeholder", "documentation_quality"],
    "properties": {
        "summary": {"type": "string", "description": "One or two plain sentences (<= 280 chars) on what the data is and what it is for."},
        "tasks": {"type": "array", "items": {"type": "string", "enum": TASKS}, "description": "ML tasks the dataset is clearly built for (max 4). Empty if the metadata does not make the intended use clear."},
        "modalities": {"type": "array", "items": {"type": "string", "enum": MODALITIES}},
        "domains": {"type": "array", "items": {"type": "string", "enum": DOMAINS}, "description": "Subject domains (max 3)."},
        "languages": {"type": "array", "items": {"type": "string"}, "description": "ISO 639-1 codes of natural languages in the data; empty if not applicable."},
        "organizations": {"type": "array", "items": {"type": "string"}, "description": "Organisations that created/published the data (canonical names, e.g. 'Meta AI', 'Stanford University')."},
        "derived_from": {"type": "array", "items": {"type": "string"}, "description": "Canonical names of source datasets this one is built from or a subset/translation of (e.g. 'MNIST', 'Common Crawl'). Empty if none stated."},
        "papers": {"type": "array", "items": {"type": "string"}, "description": "arXiv ids (e.g. '2306.01116') or DOIs explicitly referenced."},
        "quality_flags": {"type": "array", "items": {"type": "string", "enum": FLAGS}},
        "is_test_or_placeholder": {"type": "boolean", "description": "True if this looks like a test upload, empty placeholder, or personal scratch dataset with no reuse value."},
        "documentation_quality": {"type": "integer", "description": "1 (none) to 5 (excellent) for how well the metadata documents the data."},
    },
}

SYSTEM = (
    "You are a meticulous ML data librarian. You classify dataset metadata written in the MLCommons "
    "Croissant format. Use only the evidence provided; never invent organisations, papers or sources. "
    "Prefer empty arrays over guesses. Respond with JSON matching the schema."
)


def build_digest(row: dict[str, Any], content: dict[str, Any] | None = None) -> str:
    extra = row.get("extra") or {}
    meta = row.get("source_meta") or {}
    fields = extra.get("field_names", [])[:60]
    lines = [
        f"Source platform: {row['source_id']} (id: {row.get('external_id')})",
        f"Name: {row['name']}",
    ]
    if extra.get("alternate_names"):
        lines.append(f"Alternate names: {', '.join(extra['alternate_names'][:3])}")
    lines += [
        f"Creators: {', '.join(c.get('name', '') for c in (row.get('creators') or [])[:8]) or 'n/a'}",
        f"Publisher: {extra.get('publisher') or 'n/a'}",
        f"License: {row.get('license_raw') or 'n/a'}",
        f"Keywords: {', '.join((row.get('keywords') or [])[:40]) or 'n/a'}",
        f"Encoding formats: {', '.join((row.get('encoding_formats') or [])[:10]) or 'n/a'}",
        f"Record sets: {row.get('num_record_sets')}, fields: {row.get('num_fields')}; sample field names: {', '.join(fields) or 'n/a'}",
        f"Data types: {', '.join(extra.get('data_types', [])) or 'n/a'}",
        f"Size category: {extra.get('size_category') or 'n/a'}; total bytes: {row.get('total_bytes') or 'n/a'}",
        f"Identifiers: {row.get('identifiers') or {}}",
        f"Cite as: {(extra.get('cite_as') or 'n/a')[:500]}",
    ]
    pop = {k: meta.get(k) for k in ("downloads", "likes", "votes") if meta.get(k) is not None}
    if pop:
        lines.append(f"Popularity: {pop}")
    desc = markdown_to_text(row.get("description") or "")
    lines.append("Description:\n" + (desc[:3500] + (" …[truncated]" if len(desc) > 3500 else "") if desc else "(none)"))
    return "\n".join(lines)


def _grounded(value: str, evidence: str) -> bool:
    """True if a meaningful token of ``value`` occurs in the evidence text (anti-hallucination)."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", value.lower()) if len(t) >= 4 or t.isdigit()]
    return any(t in evidence for t in tokens) if tokens else value.lower() in evidence


def _ground(out: dict[str, Any], digest: str) -> dict[str, Any]:
    ev = digest.lower()
    out["papers"] = [p for p in out["papers"]
                     if (m := re.search(r"\d{4}\.\d{4,5}|10\.\d{4,9}/\S+", p)) and m.group(0).lower() in ev]
    out["organizations"] = [o for o in out["organizations"] if _grounded(o, ev)]
    out["derived_from"] = [d for d in out["derived_from"] if _grounded(d, ev)]
    return out


def _clean(out: dict[str, Any]) -> dict[str, Any]:
    out["summary"] = (out.get("summary") or "").strip()[:400]
    out["tasks"] = [t for t in out.get("tasks", []) if t in TASKS][:4]
    out["modalities"] = [m for m in out.get("modalities", []) if m in MODALITIES][:4]
    out["domains"] = [d for d in out.get("domains", []) if d in DOMAINS][:3]
    out["languages"] = sorted({lang.strip().lower()[:3] for lang in out.get("languages", []) if lang and len(lang.strip()) <= 3})[:20]
    out["organizations"] = [o.strip()[:120] for o in out.get("organizations", []) if o and o.strip()][:10]
    out["derived_from"] = [d.strip()[:120] for d in out.get("derived_from", []) if d and d.strip()][:10]
    out["papers"] = [p.strip()[:120] for p in out.get("papers", []) if p and p.strip()][:10]
    out["quality_flags"] = [f for f in out.get("quality_flags", []) if f in FLAGS]
    try:
        out["documentation_quality"] = max(1, min(5, int(out.get("documentation_quality", 1))))
    except (TypeError, ValueError):
        out["documentation_quality"] = 1
    out["is_test_or_placeholder"] = bool(out.get("is_test_or_placeholder", False))
    return out


async def links_needing_enrichment(limit: int, source_id: str | None = None) -> list[int]:
    rows = await db.fetch_all(
        """
        SELECT d.link_id FROM datasets d
        JOIN documents doc ON doc.id = d.document_id
        JOIN links l ON l.id = d.link_id
        WHERE d.validation_status <> 'not_croissant'
          AND (%(src)s::text IS NULL OR d.source_id = %(src)s)
          AND NOT EXISTS (SELECT 1 FROM enrichments e WHERE e.link_id = d.link_id
                          AND e.sha256 = doc.sha256 AND e.prompt_version = %(pv)s)
        ORDER BY l.priority DESC, d.quality_score DESC
        LIMIT %(limit)s
        """,
        {"src": source_id, "pv": PROMPT_VERSION, "limit": limit},
    )
    return [r["link_id"] for r in rows]


async def enrich_link(link_id: int, client: OpenRouter | None = None) -> dict[str, Any]:
    row = await db.fetch_one(
        """SELECT d.*, l.external_id, l.source_meta, doc.sha256 FROM datasets d
           JOIN links l ON l.id = d.link_id JOIN documents doc ON doc.id = d.document_id
           WHERE d.link_id = %s""",
        (link_id,),
    )
    if row is None:
        return {"link_id": link_id, "status": "missing"}
    cached = await db.fetch_one(
        "SELECT id FROM enrichments WHERE link_id = %s AND sha256 = %s AND prompt_version = %s",
        (link_id, row["sha256"], PROMPT_VERSION),
    )
    if cached:
        return {"link_id": link_id, "status": "cached"}
    own = client is None
    client = client or OpenRouter()
    digest = build_digest(row)
    try:
        res = await client.structured(SYSTEM, digest, "dataset_enrichment", SCHEMA)
    except LLMError as exc:
        log.warning("enrichment failed for %s: %s", link_id, exc)
        return {"link_id": link_id, "status": "error", "error": str(exc)[:300]}
    finally:
        if own:
            await client.aclose()
    out = _ground(_clean(res.data), digest)
    await db.execute(
        """INSERT INTO enrichments (link_id, sha256, prompt_version, model, output, input_tokens, output_tokens, cost_usd)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (link_id, sha256, prompt_version) DO UPDATE SET output = EXCLUDED.output,
             model = EXCLUDED.model, created_at = now()""",
        (link_id, row["sha256"], PROMPT_VERSION, res.model, jsonb(out), res.input_tokens, res.output_tokens, res.cost_usd),
    )
    await db.execute("UPDATE datasets SET indexed_at = NULL WHERE link_id = %s", (link_id,))
    await db.execute(
        "INSERT INTO crawl_events (source_id, link_id, kind, detail) VALUES (%s, %s, 'enriched', %s)",
        (row["source_id"], link_id, jsonb({"model": res.model, "cost": res.cost_usd})),
    )
    return {"link_id": link_id, "status": "ok", "cost_usd": res.cost_usd, "model": res.model}
