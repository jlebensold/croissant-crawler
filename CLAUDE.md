# CLAUDE.md

Guidance for agents working in this repository.

## What this is
Croissant Crawler: discover → fetch → validate → store → index → enrich (LLM) → graph, for MLCommons
Croissant dataset metadata. Design: `docs/TDD.md`. Requirements: `docs/PRD.md`. A local `ref/`
directory, if present, holds a private earlier prototype: it is git-ignored, never import from it
and never commit it.

## Commands
- `uv sync` installs dependencies. Use uv only, never poetry or pip.
- `docker compose up -d` runs the full stack (web :8000, Temporal UI :8233, Typesense :8108, Postgres :5432).
- `uv run pytest -m "not integration"` runs fast tests. `uv run pytest` also runs integration tests (needs postgres + typesense).
- `uv run ruff check src tests` lints.
- `uv run croissant-crawler --help` shows the CLI. `run` executes the pipeline without Temporal.

## Conventions
- Business logic lives in `pipeline/*` as plain async functions that are idempotent and take IDs.
  `orchestration/activities.py` only adapts them. Keep workflows deterministic: no I/O, imports
  only via `workflow.unsafe.imports_passed_through()`.
- Changing workflow *structure* (activity order or names) breaks replay of running workflows.
  Bump workflow names or let runs finish first.
- SQL is plain psycopg 3 with dict rows. Schema changes go in a new `migrations/NNN_*.sql` file;
  never edit an applied migration.
- Validator or rubric changes: bump `RUBRIC_VERSION` (quality.py), then run `croissant-crawler revalidate`.
- LLM prompt or schema changes: bump `PROMPT_VERSION` (enrich.py). The cache is keyed on it.
- Never send `temperature` (or other sampling params) to OpenRouter. With `require_parameters`,
  it silently routes to the fallback model.
- Upstream text is untrusted. Templates rely on Jinja autoescape; search snippets use the
  marker-then-escape approach in `search_service.py`.
- Be polite to upstreams. New sources need an entry in `SOURCE_RATES` and must use `PoliteClient`.
