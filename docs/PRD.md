# Croissant Crawler — Product Requirements Document

| | |
|---|---|
| **Status** | v0.1, working prototype |
| **Owner** | [jlebensold/croissant-crawler](https://github.com/jlebensold/croissant-crawler) (MIT) |
| **Last updated** | 2026-11-08 |
| **Companion doc** | [Technical Design Document](TDD.md) |

## 1. Summary

Croissant Crawler is a self-hostable stack that finds, validates, stores, enriches and indexes
[MLCommons Croissant](https://mlcommons.org/croissant/) dataset metadata from the major data
platforms, and serves it through a search UI, a knowledge graph and a JSON API.

The whole system runs on one machine with `docker compose up`. It follows the pipeline sketched
on the whiteboard:

```
repos ──▶ links (+timestamps) ──▶ validate ──▶ Croissant store ──▶ index ──▶ LLM ──▶ graph
```

## 2. Problem

Croissant is now published by Hugging Face, Kaggle, OpenML and Dataverse, among others. That makes
dataset metadata machine-readable, but nobody can easily answer these questions today:

* Which datasets across *all* platforms fit my task, modality, language and license?
* Is a dataset's Croissant file actually valid, and how complete is its metadata?
* Is this Kaggle dataset the same as that Hugging Face one? What was it derived from?
* How is Croissant adoption and quality evolving per platform?

Every platform's search is siloed, and nobody checks the published files against the spec.
An earlier internal prototype showed that crawling is feasible. It also showed what goes wrong
when crawl state lives in files keyed by encoded URLs, when URL lists are static files, and when
rate-limiting knobs are scattered across modules (see TDD §2).

## 3. Goals and non-goals

### Goals

1. **Coverage.** Continuously discover Croissant files from Hugging Face, OpenML, Kaggle, Dataverse
   instances and arbitrary web URLs. New sources plug in as small adapters.
2. **Trust.** Validate every file with the reference `mlcroissant` validator. Score metadata quality
   with a transparent, versioned rubric. Never hide why a score is what it is.
3. **History.** Keep every version of every Croissant file, content-addressed and timestamped,
   together with when each link was discovered, last seen, fetched and changed.
4. **Findability.** Offer fast faceted, full-text and semantic search, with quality-aware ranking
   that pushes placeholder or test uploads down.
5. **Understanding.** Add LLM-generated summaries and controlled-vocabulary facets (tasks,
   modalities, domains). Link datasets to organisations, licenses, papers, lineage and
   cross-platform duplicates in a knowledge graph.
6. **Operability.** Run locally with one command. Stay polite to upstreams. Survive crashes and
   resume. Be observable. Keep LLM costs bounded and predictable.
7. **Openness.** MIT-licensed code, exportable data (JSONL corpus, graph JSON) and a documented API.

### Non-goals (v0.x)

* Downloading or mirroring the datasets themselves. We store metadata only.
* Hosting a public multi-tenant service. Deployments are self-hosted, and auth is out of scope.
* Editing or fixing upstream Croissant files. We report problems; we do not rewrite them.
* Model cards and model metadata. The prototype mixed these in; this product is about datasets.

## 4. Users

| Persona | Needs | Primary surfaces |
|---|---|---|
| **ML practitioner / researcher** | Find datasets by task, modality, language and license across platforms; judge fitness quickly | Search, dataset page, similar datasets |
| **Dataset publisher / platform engineer** | See whether their Croissant validates and what would raise its quality | Validator, quality breakdown, crawl dashboard errors |
| **Croissant / MLCommons working group** | Measure adoption, spec-conformance and common errors per platform | Crawl dashboard, exports, graph |
| **Agent / tool builder** | Programmatic search and metadata retrieval | JSON API, JSONL export, graph export |
| **Operator** | Run, schedule, monitor and extend the crawler | `docker compose`, CLI, Temporal UI, dashboard |

## 5. User stories

* As a practitioner, I search "speech recognition in Swahili" and get relevant audio datasets even
  when the words don't match exactly, filtered to commercially usable licenses.
* As a practitioner, I open a dataset and see a plain-language summary, its schema (record sets and
  fields), its license, its quality score and why, and which datasets are similar or duplicates.
* As a publisher, I paste my Croissant URL into the validator and get errors, warnings and a
  checklist of what would raise my score. Then I add it to the index.
* As a WG member, I see per-platform validity rates and the most common spec errors.
* As an operator, I run `docker compose up`, click **Start workflow**, watch progress live, and
  schedule a nightly re-crawl that only refetches what changed.
* As a tool builder, I call `/api/search?q=…&modalities=image` and `/api/datasets/{id}/croissant`.

## 6. Functional requirements

Priorities: **P0** is required for v0.1 (all implemented), **P1** is next, **P2** is later.

### 6.1 Discovery ("repos → links")
| ID | Requirement | Pri |
|---|---|---|
| D1 | Adapters for Hugging Face (`library:mlcroissant` filter), OpenML, Kaggle and Dataverse (multiple instances) | P0 |
| D2 | Generic web source: seed URLs, sitemaps and HTML pages with embedded Croissant JSON-LD; user submissions | P0 |
| D3 | Each link records `discovered_at`, `last_seen_at`, the upstream revision marker and popularity signals | P0 |
| D4 | Resumable discovery: cursors are checkpointed in the DB | P0 |
| D5 | Detect links that disappear from listings and mark them gone after N misses | P1 |
| D6 | More sources: Zenodo, data.europa.eu, TFDS catalogue, NASA, Papers with Code successors | P2 |

### 6.2 Fetch and change detection
| ID | Requirement | Pri |
|---|---|---|
| F1 | Per-source rate limits, honouring `Retry-After` and the RateLimit headers | P0 |
| F2 | Skip the fetch when the upstream revision marker is unchanged; use conditional GET otherwise | P0 |
| F3 | Adaptive revisit interval: changed means revisit soon, unchanged means back off up to a maximum | P0 |
| F4 | Circuit breaker: an unhealthy upstream is paused instead of hammered | P0 |
| F5 | Optional platform credentials (`HF_TOKEN`, Kaggle) to raise limits | P0 |
| F6 | robots.txt evaluation for the generic web source | P1 |

### 6.3 Validation and quality
| ID | Requirement | Pri |
|---|---|---|
| V1 | Tiered validation: parse → Croissant shape → `mlcroissant` errors and warnings | P0 |
| V2 | Quality score from 0 to 100 with per-check breakdown and tiers A–F; invalid documents are capped | P0 |
| V3 | Re-validate everything offline when the validator or rubric version changes | P0 |
| V4 | Liveness probes of `contentUrl`s (sampled HEAD requests) feeding the score | P1 |
| V5 | Publisher-facing "how to improve" suggestions per failed check | P1 |

### 6.4 Croissant store
| ID | Requirement | Pri |
|---|---|---|
| S1 | Immutable versions keyed by SHA-256 of canonical JSON, with fetch timestamps | P0 |
| S2 | Raw JSON-LD of current and historical versions available via the API | P0 |
| S3 | JSONL(.gz) export of the current corpus | P0 |
| S4 | Version diffs in the UI | P2 |

### 6.5 Index and search
| ID | Requirement | Pri |
|---|---|---|
| I1 | Typesense collection with facets for source, license, task, modality, domain, tier, validity, Croissant version, language and format | P0 |
| I2 | Hybrid keyword and embedding search using a built-in local model, with no external API | P0 |
| I3 | Quality- and popularity-aware default ranking; placeholders hidden by default | P0 |
| I4 | "Similar datasets" via vector neighbours | P0 |
| I5 | Saved searches and alerts | P2 |

### 6.6 LLM enrichment
| ID | Requirement | Pri |
|---|---|---|
| L1 | Summary, tasks, modalities, domains, languages, organisations, lineage, papers, quality flags and placeholder detection, via JSON-schema structured output | P0 |
| L2 | Grounding: drop entities and IDs not present in the source text | P0 |
| L3 | Cache by (document hash, prompt version) so unchanged documents are never re-sent | P0 |
| L4 | Configurable model with fallbacks through OpenRouter; record cost per call | P0 |
| L5 | Evaluation set with human labels to track enrichment precision per prompt version | P1 |

### 6.7 Knowledge graph
| ID | Requirement | Pri |
|---|---|---|
| G1 | Nodes for datasets, sources, organisations, people, licenses, tasks, modalities, domains, papers and external datasets | P0 |
| G2 | Edges: PUBLISHED_ON, CREATED_BY, LICENSED_UNDER, HAS_TASK, HAS_MODALITY, IN_DOMAIN, CITES, DERIVED_FROM, SAME_AS, SIMILAR_TO | P0 |
| G3 | Interactive explorer (overview of hubs, focus and expand) and JSON export | P0 |
| G4 | Entity resolution for organisations (aliases such as "FAIR" and "Meta AI") | P1 |

### 6.8 Surfaces
| ID | Requirement | Pri |
|---|---|---|
| U1 | Lightweight web UI with search, dataset page, graph, crawl dashboard, validator and about pages, with no JS build step | P0 |
| U2 | JSON API with OpenAPI docs | P0 |
| U3 | CLI covering every pipeline stage, with and without Temporal | P0 |
| U4 | Admin actions (start a pipeline) behind a config flag | P0 |

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| **Local-first** | One `docker compose up` brings up everything on a laptop (about 2 GB RAM). No cloud account is needed; the LLM is optional. |
| **Politeness** | Never exceed configured per-source request rates, globally across all workers. Respect upstream rate-limit signals. |
| **Durability** | Crashes and restarts resume where they left off. Every stage is idempotent. |
| **Scale target** | 100k datasets on a single machine. 1M with horizontally scaled workers and managed Postgres and Typesense. |
| **Latency** | Search p95 under 150 ms at 100k documents. Dataset page under 200 ms. |
| **Cost** | LLM cost under $0.001 per new or changed dataset. Recurring crawls cost about $0 when nothing changed. |
| **Observability** | Workflow history in the Temporal UI, a crawl event log and dashboard, structured logs. |
| **Security** | Secrets only via env. All upstream content is HTML-escaped in the UI. Admin actions can be disabled. The User-Agent contains no personal data. |
| **Portability** | Python 3.12, Postgres 16, Typesense 29, Temporal 1.x. Linux/amd64 and arm64. |

## 8. Success metrics

| Metric | v0.1 target | How measured |
|---|---|---|
| Valid Croissant documents indexed | ≥ 1,000 (achieved: 3,069 valid of 3,427 on the first run) | `/api/stats` |
| Sources live | ≥ 4 | dashboard |
| Recrawl efficiency | ≥ 90% of revisits avoid a download (skipped or 304) | `crawl_events` |
| Freshness | median age of the current version < 7 days for active datasets | `links.last_fetched_at` |
| Search relevance | nDCG@10 ≥ 0.7 on a 50-query judged set | eval script (P1) |
| Enrichment precision | ≥ 90% correct tasks and modalities on 200 labelled datasets | eval set (P1) |
| LLM cost | < $1 per 1,000 enriched datasets | `enrichments.cost_usd` |

## 9. Release plan

* **M0, prototype (this release).** Four platforms plus the web source, the full pipeline, UI, API,
  CLI, Temporal workflows and schedule, more than 1,000 documents, and tests.
* **M1, quality.** Evaluation sets for search and enrichment, liveness probes, improvement
  suggestions, org entity resolution, robots.txt support for the web source.
* **M2, scale.** Multi-instance Dataverse discovery, Zenodo and data.europa.eu, read replicas, a
  Typesense cluster, a public read-only deployment profile.
* **M3, ecosystem.** Version diffs, alerts, an MCP server for agents, and a periodic public corpus
  release (JSONL plus graph).

## 10. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Upstream outages. During the first crawl, OpenML's Croissant converter returned HTTP 500 for nearly every dataset. | Partial coverage | Circuit breaker, deferred retries without penalty, per-source isolation in child workflows |
| Rate limiting or blocking (HF allows 500 requests per 5 minutes anonymously) | Slow crawls | Listing-level revision markers avoid refetches; optional tokens; global task-queue rate limits |
| Spec drift (Croissant 1.0 → 1.1) | False invalids | Validator version stored per result; `revalidate` command; keep `mlcroissant` pinned and upgradable |
| LLM hallucination | Wrong facets and entities | Strict schemas with enums, grounding filter, prompt versioning, evaluation set (P1) |
| LLM cost blow-up | Budget | Content-hash cache, cheap default model, per-run limits, cost recorded per call |
| Metadata licensing and ToS | Legal | Store and redistribute metadata only, link back to sources, honour opt-outs (web source) |
| Placeholder spam on open platforms | Noisy results | LLM placeholder flag, rank penalty, hidden by default |

## 11. Open questions

1. Should the public corpus export include documents that fail validation? Today the default is
   valid documents only.
2. Should SAME_AS edges be confirmed by the LLM, or by a human curation queue, before being shown
   as "also published as"?
3. Which additional Dataverse instances have the Croissant exporter enabled? We need a registry probe.
4. Should we contribute validation findings back upstream, for example aggregated error reports
   to platform maintainers?
