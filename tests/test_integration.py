"""End-to-end pipeline against real Postgres + Typesense (docker compose up postgres typesense).

Uses an isolated database (crawler_test) and Typesense collection; upstream HTTP is mocked.
Run with:  uv run pytest -m integration
"""

import copy
import os
import uuid

import httpx
import psycopg
import pytest
import respx

from .conftest import load

pytestmark = pytest.mark.integration

ADMIN_URL = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://crawler:crawler@localhost:5432/crawler")
TEST_DB = "crawler_test"


@pytest.fixture
async def env(monkeypatch):
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable: {exc}")
    collection = f"datasets_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("DATABASE_URL", ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}")
    monkeypatch.setenv("TYPESENSE_COLLECTION", collection)
    monkeypatch.setenv("TYPESENSE_EMBEDDING_MODEL", "")
    monkeypatch.setenv("SOURCE_RATES", '{"huggingface": 1000, "web": 1000}')
    from croissant_crawler import config, db
    from croissant_crawler.pipeline import crawl

    config.get_settings.cache_clear()
    crawl._clients.clear()
    await db.migrate()
    yield collection
    from croissant_crawler.search import Typesense

    async with Typesense() as ts:
        try:
            await ts._client.delete(f"/collections/{collection}")
        except Exception:
            pass
    await db.close_pool()
    config.get_settings.cache_clear()
    crawl._clients.clear()


def _listing(sha_mnist="s1"):
    return [
        {"id": "ylecun/mnist", "sha": sha_mnist, "downloads": 1000, "likes": 5},
        {"id": "openai/gsm8k", "sha": "g1", "downloads": 500},
        {"id": "gone/missing", "sha": "m1", "downloads": 1},
    ]


async def test_pipeline_end_to_end(env):
    from croissant_crawler import db
    from croissant_crawler.orchestration.local import crawl_source
    from croissant_crawler.pipeline import graph, index
    from croissant_crawler.search import Typesense

    mnist, gsm8k = load("hf_mnist.json"), load("hf_gsm8k.json")
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="localhost").pass_through()
        mock.route(host="127.0.0.1").pass_through()
        listing = mock.get("https://huggingface.co/api/datasets").mock(return_value=httpx.Response(200, json=_listing()))
        mnist_route = mock.get("https://huggingface.co/api/datasets/ylecun/mnist/croissant").mock(return_value=httpx.Response(200, json=mnist))
        mock.get("https://huggingface.co/api/datasets/openai/gsm8k/croissant").mock(return_value=httpx.Response(200, json=gsm8k))
        mock.get("https://huggingface.co/api/datasets/gone/missing/croissant").mock(return_value=httpx.Response(404))

        stats = await crawl_source("huggingface", limit=10)
        assert stats["discover_new"] == 3 and stats["fetch_ok"] == 2 and stats["fetch_not_found"] == 1
        assert (await db.fetch_one("SELECT count(*) AS n FROM datasets"))["n"] == 2
        assert (await db.fetch_one("SELECT count(*) AS n FROM documents"))["n"] == 2

        # 2nd pass: upstream revision unchanged -> no HTTP fetch, no new versions
        await db.execute("UPDATE links SET next_fetch_at = now()")
        calls = mnist_route.call_count
        stats = await crawl_source("huggingface", limit=10)
        assert stats.get("fetch_skipped", 0) == 2 and mnist_route.call_count == calls

        # upstream changes: new sha in listing + new content -> a second immutable version
        changed = copy.deepcopy(mnist)
        changed["description"] = "MNIST, now with a better description " * 10
        listing.mock(return_value=httpx.Response(200, json=_listing(sha_mnist="s2")))
        mnist_route.mock(return_value=httpx.Response(200, json=changed))
        await crawl_source("huggingface", limit=10)  # discovery makes the changed link due immediately
        versions = await db.fetch_all(
            "SELECT d.id FROM documents d JOIN links l ON l.id = d.link_id WHERE l.external_id = 'ylecun/mnist'")
        assert len(versions) == 2
        row = await db.fetch_one("SELECT description, quality_score FROM datasets d JOIN links l ON l.id = d.link_id WHERE l.external_id = 'ylecun/mnist'")
        assert row["description"].startswith("MNIST, now with")

    await index.index_pending()
    async with Typesense() as ts:
        res = await ts.search({"q": "mnist", "query_by": "name,description"})
    assert res["found"] >= 1 and res["hits"][0]["document"]["external_id"] == "ylecun/mnist"

    g = await graph.rebuild_graph()
    assert g["nodes"] > 2 and g.get("LICENSED_UNDER", 0) == 2
