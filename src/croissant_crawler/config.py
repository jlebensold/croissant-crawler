"""Runtime configuration, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- storage -----------------------------------------------------------------
    database_url: str = "postgresql://crawler:crawler@localhost:5432/crawler"

    # --- search ------------------------------------------------------------------
    typesense_url: str = "http://localhost:8108"
    typesense_api_key: str = "croissant-dev-key"
    typesense_collection: str = "datasets"
    # Built-in Typesense embedding model (runs inside the Typesense container).
    # Set to "" to disable semantic search entirely.
    typesense_embedding_model: str = "ts/all-MiniLM-L12-v2"

    # --- orchestration -----------------------------------------------------------
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "croissant-main"
    temporal_ui_url: str = "http://localhost:8233"

    # --- LLM enrichment ----------------------------------------------------------
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "anthropic/claude-haiku-5.5"
    llm_fallback_models: list[str] = Field(default_factory=lambda: ["google/gemini-2.5-flash-lite"])
    llm_concurrency: int = 12
    llm_rate_per_second: float = 6.0

    # --- crawling ----------------------------------------------------------------
    # Generic UA without personal contact details; override to identify your deployment.
    user_agent: str = "croissant-crawler/0.1 (+https://github.com/jlebensold/croissant-crawler)"
    http_timeout_seconds: float = 30.0
    max_document_bytes: int = 25_000_000
    sources_enabled: list[str] = Field(
        default_factory=lambda: ["huggingface", "openml", "kaggle", "dataverse", "web"]
    )
    # Requests per second per source, enforced both in-process and per Temporal task queue.
    source_rates: dict[str, float] = Field(
        default_factory=lambda: {
            "huggingface": 1.5,
            "openml": 3.0,
            "kaggle": 1.0,
            "dataverse": 2.0,
            "web": 2.0,
        }
    )
    hf_token: str = ""
    dataverse_instances: list[str] = Field(
        default_factory=lambda: ["https://dataverse.harvard.edu"]
    )
    # Recrawl policy (adaptive): unchanged documents back off up to the max interval.
    recrawl_min_hours: float = 24.0
    recrawl_max_days: float = 30.0

    # --- web ---------------------------------------------------------------------
    web_title: str = "Croissant Index"
    enable_admin_actions: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
