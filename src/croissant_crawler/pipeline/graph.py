"""Knowledge graph over the index.

Nodes: dataset, source, organization, person, license, task, modality, domain, paper,
       external_dataset (a derived-from reference we could not resolve to a crawled dataset).
Edges: PUBLISHED_ON, CREATED_BY, LICENSED_UNDER, HAS_TASK, HAS_MODALITY, IN_DOMAIN, CITES,
       DERIVED_FROM, SAME_AS (same dataset on different platforms), SIMILAR_TO (embeddings).

The graph is rebuilt from the store in one transaction; it is cheap (seconds for 10^4 datasets)
and keeps the graph a pure function of the crawled + enriched data.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

import orjson

from .. import db
from ..search import Typesense
from .index import DATASET_SELECT

log = logging.getLogger(__name__)

GENERIC_NAMES = {"data", "dataset", "datasets", "train", "test", "default", "sample", "demo", "example",
                 "untitled", "my dataset", "new dataset", "eval", "results", "output", "temp", "tmp"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:120]


def name_key(text: str) -> str:
    """Normalised name for cross-source matching (drops org prefixes and punctuation)."""
    t = text.lower().split("/")[-1]
    t = re.sub(r"\b(dataset|data|the|v\d+(\.\d+)*)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def _norm_arxiv(p: str) -> str | None:
    m = re.search(r"(\d{4}\.\d{4,5})", p)
    return m.group(1) if m else None


class GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, tuple[str, str, dict]] = {}
        self.edges: dict[tuple[str, str, str], tuple[float, dict]] = {}

    def node(self, nid: str, ntype: str, label: str, **props: Any) -> str:
        if nid not in self.nodes:
            self.nodes[nid] = (ntype, label[:200], props)
        return nid

    def edge(self, src: str, dst: str, etype: str, weight: float = 1.0, **props: Any) -> None:
        if src == dst:
            return
        key = (src, dst, etype)
        prev = self.edges.get(key)
        if prev is None or prev[0] < weight:
            self.edges[key] = (weight, props)


def build(rows: list[dict], similarities: list[dict]) -> GraphBuilder:
    g = GraphBuilder()
    by_key: dict[str, list[str]] = defaultdict(list)
    ext_index: dict[str, str] = {}

    for r in rows:
        did = f"dataset:{r['link_id']}"
        g.node(did, "dataset", r["name"], source=r["source_id"], tier=r["quality_tier"],
               quality=r["quality_score"], external_id=r["external_id"], popularity=r["popularity"])
        ext = r["external_id"] if not r["external_id"].startswith("http") else None  # URLs aren't names
        for alias in [r["name"], ext, *(r.get("extra") or {}).get("alternate_names", [])]:
            k = name_key(alias or "")
            if len(k) >= 3 and k not in GENERIC_NAMES:
                if did not in by_key[k]:
                    by_key[k].append(did)
                ext_index.setdefault(k, did)

    for r in rows:
        did = f"dataset:{r['link_id']}"
        enr = r.get("enrichment") or {}
        extra = r.get("extra") or {}
        g.edge(did, g.node(f"source:{r['source_id']}", "source", r["source_id"]), "PUBLISHED_ON")
        if r["license"] and r["license"] not in ("Unknown",):
            g.edge(did, g.node(f"license:{r['license']}", "license", r["license"]), "LICENSED_UNDER")

        orgs = list(enr.get("organizations") or [])
        for c in r["creators"] or []:
            name = (c.get("name") or "").strip()
            if not name or name.startswith("http"):
                continue
            if c.get("type") == "Person":
                g.edge(did, g.node(f"person:{slug(name)}", "person", name), "CREATED_BY")
            else:
                orgs.append(name)
        if extra.get("publisher") and extra["publisher"].lower() not in ("kaggle", "harvard dataverse", "hugging face", "openml"):
            orgs.append(extra["publisher"])
        for org in orgs:
            if slug(org):
                g.edge(did, g.node(f"org:{slug(org)}", "organization", org), "CREATED_BY")

        tasks = set(enr.get("tasks") or []) | set(extra.get("tasks") or [])
        for t in tasks:
            g.edge(did, g.node(f"task:{t}", "task", t), "HAS_TASK")
        for m in set(enr.get("modalities") or []) | set(extra.get("modalities") or []):
            g.edge(did, g.node(f"modality:{m}", "modality", m), "HAS_MODALITY")
        for d in enr.get("domains") or []:
            g.edge(did, g.node(f"domain:{d}", "domain", d), "IN_DOMAIN")

        arxiv = set((r.get("identifiers") or {}).get("arxiv", []))
        arxiv |= {a for a in (_norm_arxiv(p) for p in enr.get("papers") or []) if a}
        for a in arxiv:
            g.edge(did, g.node(f"paper:arxiv:{a}", "paper", f"arXiv:{a}", url=f"https://arxiv.org/abs/{a}"), "CITES")

        for ref in enr.get("derived_from") or []:
            k = name_key(ref)
            if len(k) < 3 or k in GENERIC_NAMES:
                continue
            target = ext_index.get(k)
            if target and target != did:
                g.edge(did, target, "DERIVED_FROM", resolved=True)
            elif not target:
                g.edge(did, g.node(f"external_dataset:{k}", "external_dataset", ref), "DERIVED_FROM", resolved=False)

    # SAME_AS: same normalised name on different platforms (small groups only).
    for k, members in by_key.items():
        if not 2 <= len(members) <= 6:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if g.nodes[a][2]["source"] != g.nodes[b][2]["source"]:
                    g.edge(a, b, "SAME_AS", key=k)

    for s in similarities:
        a, b = f"dataset:{s['link_id']}", f"dataset:{s['other_id']}"
        if a in g.nodes and b in g.nodes:
            src, dst = sorted((a, b))
            g.edge(src, dst, "SIMILAR_TO", weight=round(float(s["score"]), 4))
    return g


async def compute_similarities(k: int = 6, min_score: float = 0.6, limit: int = 100000) -> dict[str, int]:
    """Refresh nearest neighbours for datasets indexed since their similarities were computed."""
    rows = await db.fetch_all(
        """SELECT d.link_id FROM datasets d
           WHERE d.indexed_at IS NOT NULL AND NOT EXISTS (
             SELECT 1 FROM dataset_similarities s WHERE s.link_id = d.link_id AND s.computed_at >= d.indexed_at)
           ORDER BY d.link_id LIMIT %s""",
        (limit,),
    )
    done = 0
    async with Typesense() as ts:
        if not ts.embedding_model:
            return {"computed": 0}
        for r in rows:
            lid = r["link_id"]
            try:
                res = await ts.search({
                    "q": "*",
                    "vector_query": f"embedding:([], id: {lid}, k: {k})",
                    "exclude_fields": "embedding,description",
                    "per_page": k,
                })
            except Exception as exc:  # doc might not be in the index yet
                log.debug("similarity query failed for %s: %s", lid, exc)
                continue
            pairs = []
            for hit in res.get("hits", []):
                other = int(hit["document"]["id"])
                score = 1.0 - float(hit.get("vector_distance", 1.0))
                if other != lid and score >= min_score:
                    pairs.append((lid, other, score))
            async with db.connection() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM dataset_similarities WHERE link_id = %s", (lid,))
                    for p in pairs:
                        await conn.execute(
                            "INSERT INTO dataset_similarities (link_id, other_id, score) VALUES (%s,%s,%s) "
                            "ON CONFLICT (link_id, other_id) DO UPDATE SET score = EXCLUDED.score, computed_at = now()",
                            p,
                        )
                    if not pairs:  # sentinel so we don't recompute until the next re-index
                        await conn.execute(
                            "INSERT INTO dataset_similarities (link_id, other_id, score) VALUES (%s,%s,0) "
                            "ON CONFLICT (link_id, other_id) DO UPDATE SET computed_at = now()",
                            (lid, lid),
                        )
            done += 1
    return {"computed": done}


async def rebuild_graph() -> dict[str, int]:
    rows = await db.fetch_all(DATASET_SELECT)
    sims = await db.fetch_all("SELECT link_id, other_id, score FROM dataset_similarities WHERE link_id <> other_id AND score > 0")
    g = build(rows, sims)
    async with db.connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM graph_edges")
            await conn.execute("DELETE FROM graph_nodes")
            async with conn.cursor().copy("COPY graph_nodes (id, type, label, props) FROM STDIN") as cp:
                for nid, (ntype, label, props) in g.nodes.items():
                    await cp.write_row((nid, ntype, label, orjson.dumps(props).decode()))
            async with conn.cursor().copy("COPY graph_edges (src, dst, type, weight, props) FROM STDIN") as cp:
                for (src, dst, etype), (weight, props) in g.edges.items():
                    await cp.write_row((src, dst, etype, weight, orjson.dumps(props).decode()))
    counts: dict[str, int] = defaultdict(int)
    for (_, _, etype) in g.edges:
        counts[etype] += 1
    return {"nodes": len(g.nodes), "edges": len(g.edges), **counts}
