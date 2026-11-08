# Croissant Crawler

**A self-hostable index of ML datasets described in [MLCommons Croissant](https://mlcommons.org/croissant/).**
It crawls Hugging Face, OpenML, Kaggle, Dataverse and arbitrary web pages. It validates every
Croissant file with the reference validator, keeps every version, scores metadata quality, adds
LLM summaries and facets, links everything into a knowledge graph, and serves search, a graph
explorer, a crawl dashboard and a JSON API.

```
repos ──▶ links (+timestamps) ──▶ validate ──▶ Croissant store ──▶ index ──▶ LLM ──▶ graph
```

Everything runs locally with Docker Compose: Postgres, Typesense, a Temporal dev server, a worker
and the web app.

## Quick start

```bash
cp .env.example .env              # optional: add OPENROUTER_API_KEY for LLM enrichment
docker compose up -d --build      # postgres, typesense, temporal, worker, web
open http://localhost:8000        # UI   (Temporal UI: http://localhost:8233)
```

Start a crawl from **Crawl → Run the pipeline**, or from the CLI:

```bash
docker compose exec worker croissant-crawler workflow start --limit 1000   # durable, on Temporal
docker compose exec worker croissant-crawler workflow schedule              # nightly re-crawl
```

No Temporal? The same pipeline runs in-process:

```bash
uv sync
docker compose up -d postgres typesense
uv run croissant-crawler run huggingface kaggle --limit 200
uv run croissant-crawler web        # http://127.0.0.1:8000
```

## What you get

| Surface | What it does |
|---|---|
| **Search** (`/`) | Hybrid keyword and semantic search using Typesense with a built-in MiniLM model, so no embedding API is needed. Facets cover source, modality, task, domain, license, quality tier, validity, Croissant version, language and format. Ranking is quality- and popularity-aware, and placeholder or test uploads are hidden by default. |
| **Dataset page** | AI summary and facets, description, record sets and fields, files, `mlcroissant` errors and warnings, a 100-point quality breakdown, metadata, crawl timeline and version history with raw JSON-LD, plus same-dataset-elsewhere, lineage and similar datasets. |
| **Graph** (`/graph`) | Interactive knowledge graph of datasets, organisations, people, licenses, tasks, modalities, domains, papers, DERIVED_FROM, SAME_AS and SIMILAR_TO. Exportable as JSON. |
| **Crawl** (`/crawl`) | Per-source frontier stats, quality tier bars, validity by Croissant version, most common spec errors, live event feed, fetch failures, start-workflow form and recent Temporal runs. |
| **Validate** (`/validate`) | Paste a URL or JSON-LD to get the same validation and quality report as the crawler. One click adds it to the index. |
| **API** (`/docs`) | `/api/search`, `/api/datasets/{id}`, `/api/datasets/{id}/croissant`, `/api/graph/*`, `/api/stats`, `POST /api/validate`, `POST /api/submit`, … |
| **CLI** | `croissant-crawler migrate · discover · run · index · enrich · graph · revalidate · submit · validate · stats · export · worker · web · workflow start/schedule` |

## How it works

* **Sources** are about 80-line adapters that page through each platform's listing API and upsert
  Croissant URLs into a Postgres crawl frontier. Each link carries discovered and last-seen
  timestamps, an upstream revision marker (HF git sha, Kaggle version, Dataverse versionId) and
  popularity.
* **Fetching** is polite and cheap. Per-source Temporal task queues enforce global request rates,
  and a per-host token bucket honours `Retry-After` and HF's `RateLimit` header. A circuit breaker
  pauses unhealthy upstreams. Fetches are skipped entirely when the revision marker hasn't moved;
  otherwise conditional GETs are used. Revisit intervals adapt, from 24 h after a change up to
  30 days when stable.
* **Validation** is tiered: parse (including JSON-LD embedded in HTML), Croissant shape, then
  `mlcroissant` errors and warnings, then a transparent quality rubric (validity 25, metadata 30,
  distribution 20, structure 15, responsible AI 10; tiers A–F).
* **Croissant store**: immutable versions keyed by SHA-256 of canonical JSON, exportable with
  `croissant-crawler export`.
* **LLM enrichment** uses OpenRouter with a strict JSON schema. It is grounded against the source
  text so invented paper IDs and organisations are dropped, and cached per content hash so
  unchanged datasets are never re-sent. The default is `anthropic/claude-haiku-5.5`, with
  `google/gemini-2.5-flash-lite` as fallback.
* **Graph** is rebuilt from the store in seconds. SIMILAR_TO edges come from the embedding index,
  SAME_AS from cross-platform name matching, and DERIVED_FROM from grounded LLM lineage.

Design details and the lessons behind them are in
**[docs/TDD.md](docs/TDD.md)**. Goals, users and requirements are in **[docs/PRD.md](docs/PRD.md)**.

## Configuration

All settings are environment variables; see [`.env.example`](.env.example). The most useful ones:

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | – | Enables LLM enrichment. Without it, everything else works. |
| `LLM_MODEL`, `LLM_FALLBACK_MODELS` | `anthropic/claude-haiku-5.5`, `["google/gemini-2.5-flash-lite"]` | Any OpenRouter model with structured outputs. |
| `SOURCES_ENABLED` | `["huggingface","openml","kaggle","dataverse","web"]` | `web` re-crawls submitted URLs and any `WEB_SEEDS`. |
| `SOURCE_RATES` | HF 1.5/s, OpenML 3/s, Kaggle 1/s, Dataverse 2/s | Global per-source request rates. |
| `DATAVERSE_INSTANCES` | `["https://dataverse.harvard.edu"]` | Any Dataverse 6.x instance with the Croissant exporter. |
| `HF_TOKEN`, `KAGGLE_USERNAME`/`KAGGLE_KEY` | – | Optional; raise upstream rate limits. |
| `TYPESENSE_EMBEDDING_MODEL` | `ts/all-MiniLM-L12-v2` | `""` disables semantic search. |
| `RECRAWL_MIN_HOURS` / `RECRAWL_MAX_DAYS` | 24 / 30 | Adaptive revisit bounds. |
| `ENABLE_ADMIN_ACTIONS` | `true` | Set `false` on shared deployments. |

## Development

```bash
uv sync                                   # Python 3.12, deps from uv.lock
docker compose up -d postgres typesense temporal
uv run croissant-crawler migrate
uv run croissant-crawler worker &         # Temporal worker
uv run croissant-crawler web --reload     # UI with autoreload
make test                                 # unit + workflow tests (no services needed)
make test-all                             # + integration tests against postgres/typesense
make lint
```

Project layout:

```
src/croissant_crawler/
  sources/        # one adapter per platform (huggingface, openml, kaggle, dataverse, web)
  pipeline/       # crawl services: store, crawl, validate, extract, quality, index, enrich, graph
  orchestration/  # Temporal activities, workflows, worker, client; local asyncio runner
  web/            # FastAPI app, queries, search service, Jinja templates, static (htmx, cytoscape)
  http.py         # polite client (token buckets, retries, circuit breaker)
  search.py       # Typesense client + schema
  llm.py          # OpenRouter structured-output client
migrations/       # plain SQL, applied at startup
tests/            # unit, HTTP, sources, workflows (time-skipping), integration
docs/             # PRD.md, TDD.md
```

Adding a source means subclassing `sources.base.Source`, implementing `discover_page` and
`croissant_url`, registering it in `sources/__init__.py`, and giving it a rate in `SOURCE_RATES`.

## Results

Measured on 2026-11-08 on a laptop (Docker Compose, Apple Silicon). The run was the Temporal
pipeline at 1,200 links per source, plus the 48 MLCommons reference examples submitted through
`/api/submit`.

| Source | Datasets | Valid | Valid % | Avg quality | A | B | C | F | Croissant 1.1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Hugging Face | 1,184 | 1,180 | 99.7% | 71.4 | 0 | 1,095 | 85 | 4 | 851 |
| Kaggle | 1,168 | 1,051 | 90.0% | 74.8 | 468 | 539 | 44 | 117 | 0 |
| Dataverse (Harvard) | 1,027 | 790 | 76.9% | 70.4 | 154 | 636 | 0 | 237 | 0 |
| Web (MLCommons examples) | 46 | 46 | 100% | 72.4 | 5 | 34 | 7 | 0 | 14 |
| OpenML | 2 | 2 | 100% | 82.0 | 1 | 1 | 0 | 0 | 0 |
| **Total** | **3,427** | **3,069** | **89.6%** | **72.3** | 628 | 2,305 | 136 | 358 | 865 |

* **Frontier**: 6,450 links discovered; 3,428 Croissant documents stored. 214 links were
  forbidden (gated HF datasets, restricted Dataverse files) and 33 returned 404.
* **Enrichment**: all 3,427 datasets enriched, about 2,650 input and 300 output tokens each. Total
  OpenRouter spend was about **$1.44**, roughly $0.0004 per dataset. One Claude refusal
  (a false positive on a microbiology dataset) fell back to Gemini automatically. 57 datasets
  were flagged as placeholder or test uploads and are hidden from search by default.
* **Graph**: 8,767 nodes and 38,311 edges: 3,427 datasets, 2,309 people, 1,158 organisations,
  533 papers, 1,202 external lineage references, 8,118 SIMILAR_TO edges, 1,739 DERIVED_FROM edges
  and 40 cross-platform SAME_AS edges (Titanic, IMDb, UrbanSound8K, LFW, PlantVillage, MNIST, …).
  A full rebuild, including 3,427 vector-neighbour queries, takes about 10 s.
* **Performance**: search takes 1–10 ms in Typesense; the 3,427 dataset pages each render in about
  5 ms; re-validating and re-indexing everything offline takes about 3 minutes. The stack uses
  about 1.4 GB of RAM, most of it Typesense with the embedding model. Postgres holds 82 MB.
* **Corpus export**: `croissant-crawler export` wrote 3,069 valid documents to a 6.6 MB `.jsonl.gz`.

What the validator found:

* **Dataverse** exports often use file names with spaces as `@id`s, which mlcroissant rejects.
  That accounts for most of its 237 invalid files; the rest lack `source` field references.
* **Kaggle** sometimes emits `extract` without a `column`, `fileProperty` or `jsonPath`
  (about 75 files).
* **Hugging Face** files are almost all valid, but none reach tier A. They omit `citeAs`,
  `version`, dates and Responsible-AI fields, and the validator warns about the missing
  recommended properties on every file. 72% are already Croissant 1.1.
* **OpenML**'s Croissant service was failing upstream during this run. It returned HTTP 500 with
  an internal storage-resolution error for nearly all datasets; only cached ones such as #31
  and #40945 worked. The circuit breaker paused the source after a few failures, and the
  nightly schedule will pick OpenML back up when it recovers.

## License

MIT, see [LICENSE](LICENSE). Crawled metadata remains under the terms set by each source platform and dataset;
this project stores and links metadata only.
