.PHONY: up down logs crawl crawl-local test test-all lint fmt stats export reindex

LIMIT ?= 1000

up:            ## start the whole stack (postgres, typesense, temporal, worker, web)
	docker compose up -d --build
	@echo "UI: http://localhost:8000   Temporal: http://localhost:8233"

down:
	docker compose down

logs:
	docker compose logs -f worker web

crawl:         ## durable pipeline run on Temporal (LIMIT links per source)
	docker compose exec worker croissant-crawler workflow start --limit $(LIMIT)

crawl-local:   ## same pipeline, in-process (no Temporal)
	docker compose exec worker croissant-crawler run --limit $(LIMIT)

stats:
	docker compose exec worker croissant-crawler stats

export:
	docker compose exec worker croissant-crawler export --out /tmp/croissant.jsonl.gz
	docker compose cp worker:/tmp/croissant.jsonl.gz ./croissant.jsonl.gz

reindex:
	docker compose exec worker croissant-crawler index --rebuild

test:          ## unit + workflow tests (no services needed)
	uv run pytest -m "not integration"

test-all:      ## includes integration tests (needs: docker compose up -d postgres typesense)
	uv run pytest

lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests
