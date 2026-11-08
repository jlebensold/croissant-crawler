-- Croissant Crawler schema v1.
-- Pipeline: sources -> links (+timestamps) -> documents (croissant store) -> validations
--           -> datasets (current, normalised view) -> enrichments (LLM) -> graph

CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,               -- 'huggingface', 'openml', ...
    name          TEXT NOT NULL,
    base_url      TEXT NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    discovery_cursor JSONB,                       -- resumable discovery checkpoint
    last_discovery_started_at  TIMESTAMPTZ,
    last_discovery_finished_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The crawl frontier: one row per Croissant URL we know about.
CREATE TABLE IF NOT EXISTS links (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES sources(id),
    external_id         TEXT NOT NULL,            -- e.g. HF repo id, OpenML data id, DOI
    croissant_url       TEXT NOT NULL,
    landing_url         TEXT,
    source_meta         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- listing metadata (downloads, likes ...)
    source_version      TEXT,                     -- upstream revision marker (sha, version number)
    fetched_source_version TEXT,                  -- source_version at the time of the last fetch
    popularity          DOUBLE PRECISION NOT NULL DEFAULT 0,
    priority            DOUBLE PRECISION NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',
        -- pending | ok | not_modified | not_found | forbidden | error | invalid | gone
    discovered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),   -- last time the listing returned it
    last_fetched_at     TIMESTAMPTZ,
    last_changed_at     TIMESTAMPTZ,
    next_fetch_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    recrawl_hours       DOUBLE PRECISION NOT NULL DEFAULT 24,  -- adaptive revisit interval
    lease_until         TIMESTAMPTZ,
    fetch_count         INTEGER NOT NULL DEFAULT 0,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0,
    http_status         INTEGER,
    etag                TEXT,
    last_modified       TEXT,
    final_url           TEXT,
    last_error          TEXT,
    current_document_id BIGINT,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS links_frontier_idx ON links (source_id, next_fetch_at, priority DESC);
CREATE INDEX IF NOT EXISTS links_status_idx ON links (status);
CREATE UNIQUE INDEX IF NOT EXISTS links_url_idx ON links (croissant_url);

-- The Croissant store: immutable, content-addressed versions of each document.
CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    link_id     BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    sha256      TEXT NOT NULL,                    -- hash of canonical JSON
    size_bytes  INTEGER NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    content     JSONB NOT NULL,
    UNIQUE (link_id, sha256)
);
CREATE INDEX IF NOT EXISTS documents_sha_idx ON documents (sha256);
ALTER TABLE links DROP CONSTRAINT IF EXISTS links_current_document_fk;
ALTER TABLE links ADD CONSTRAINT links_current_document_fk
    FOREIGN KEY (current_document_id) REFERENCES documents(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS validations (
    document_id        BIGINT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    validator_version  TEXT NOT NULL,
    status             TEXT NOT NULL,   -- valid | valid_with_warnings | invalid | not_croissant
    croissant_version  TEXT,
    errors             JSONB NOT NULL DEFAULT '[]'::jsonb,
    warnings           JSONB NOT NULL DEFAULT '[]'::jsonb,
    quality_score      DOUBLE PRECISION NOT NULL DEFAULT 0,
    quality_tier       TEXT NOT NULL DEFAULT 'F',
    quality_breakdown  JSONB NOT NULL DEFAULT '{}'::jsonb,
    duration_ms        INTEGER,
    validated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current, normalised view of each dataset (one per link), used by search, API and graph.
CREATE TABLE IF NOT EXISTS datasets (
    link_id            BIGINT PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE,
    document_id        BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_id          TEXT NOT NULL,
    name               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    url                TEXT,
    license            TEXT,           -- normalised (SPDX-like) license id
    license_raw        TEXT,
    creators           JSONB NOT NULL DEFAULT '[]'::jsonb,
    keywords           TEXT[] NOT NULL DEFAULT '{}',
    date_published     TIMESTAMPTZ,
    date_modified      TIMESTAMPTZ,
    version            TEXT,
    croissant_version  TEXT,
    encoding_formats   TEXT[] NOT NULL DEFAULT '{}',
    num_files          INTEGER NOT NULL DEFAULT 0,
    num_record_sets    INTEGER NOT NULL DEFAULT 0,
    num_fields         INTEGER NOT NULL DEFAULT 0,
    total_bytes        BIGINT,
    identifiers        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {doi:[], arxiv:[]}
    extra              JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_status  TEXT NOT NULL,
    quality_score      DOUBLE PRECISION NOT NULL DEFAULT 0,
    quality_tier       TEXT NOT NULL DEFAULT 'F',
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS datasets_source_idx ON datasets (source_id);
CREATE INDEX IF NOT EXISTS datasets_name_idx ON datasets (lower(name));

-- LLM enrichment, cached by document content hash + prompt version + model.
CREATE TABLE IF NOT EXISTS enrichments (
    id              BIGSERIAL PRIMARY KEY,
    link_id         BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    sha256          TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    model           TEXT NOT NULL,
    output          JSONB NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (link_id, sha256, prompt_version)
);

-- Knowledge graph (property graph in two tables; export to JSON/GraphML).
CREATE TABLE IF NOT EXISTS graph_nodes (
    id      TEXT PRIMARY KEY,            -- '<type>:<key>'
    type    TEXT NOT NULL,
    label   TEXT NOT NULL,
    props   JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS graph_nodes_type_idx ON graph_nodes (type);
CREATE TABLE IF NOT EXISTS graph_edges (
    src     TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    dst     TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    type    TEXT NOT NULL,
    weight  DOUBLE PRECISION NOT NULL DEFAULT 1,
    props   JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (src, dst, type)
);
CREATE INDEX IF NOT EXISTS graph_edges_dst_idx ON graph_edges (dst);

-- Append-only event log of crawl activity for the dashboard (bounded by retention job).
CREATE TABLE IF NOT EXISTS crawl_events (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_id   TEXT,
    link_id     BIGINT,
    kind        TEXT NOT NULL,   -- discovered | fetched | changed | unchanged | error | validated | enriched | indexed
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS crawl_events_at_idx ON crawl_events (at DESC);

-- Nearest neighbours from the embedding index (refreshed incrementally, feeds SIMILAR_TO edges).
CREATE TABLE IF NOT EXISTS dataset_similarities (
    link_id      BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    other_id     BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    score        DOUBLE PRECISION NOT NULL,   -- 1 - cosine distance
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (link_id, other_id)
);
