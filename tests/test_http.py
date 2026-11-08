import time

import httpx
import respx

from croissant_crawler.http import PoliteClient, TokenBucket


async def test_token_bucket_rate():
    bucket = TokenBucket(rate=20)
    start = time.monotonic()
    for _ in range(25):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert 0.15 < elapsed < 1.5  # 20 burst + 5 more at 20/s


@respx.mock
async def test_retry_after_429(monkeypatch):
    route = respx.get("https://example.org/c.json").mock(side_effect=[
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(200, json={"ok": True}),
    ])
    async with PoliteClient(rates={"web": 1000}, max_retries=2) as c:
        res = await c.get("https://example.org/c.json")
    assert res.ok and route.call_count == 2


@respx.mock
async def test_plain_500_retried_once_then_reported():
    route = respx.get("https://example.org/broken").mock(return_value=httpx.Response(500))
    async with PoliteClient(rates={"web": 1000}, max_retries=4) as c:
        c_bucket = c.bucket_for("https://example.org/broken")
        res = await c.get("https://example.org/broken")
    assert res.status == 500 and route.call_count == 2
    assert c_bucket.consecutive_failures == 2


@respx.mock
async def test_circuit_breaker_opens():
    respx.get(url__regex=r"https://down\.example/.*").mock(return_value=httpx.Response(500))
    async with PoliteClient(rates={"web": 1000}, max_retries=0) as c:
        bucket = c.bucket_for("https://down.example/x")
        for i in range(TokenBucket.FAILURE_THRESHOLD):
            await c.get(f"https://down.example/{i}")
        assert bucket.circuit_open
        res = await c.get("https://down.example/after")
    assert res.status == 0 and res.error.startswith("circuit open")


@respx.mock
async def test_hf_ratelimit_header_blocks_bucket():
    respx.get("https://huggingface.co/api/x").mock(
        return_value=httpx.Response(200, json=[], headers={"ratelimit": '"api";r=0;t=7'}))
    async with PoliteClient(rates={"huggingface": 1000}) as c:
        await c.get("https://huggingface.co/api/x")
        bucket = c.bucket_for("https://huggingface.co/api/x")
    assert bucket.blocked_until - time.monotonic() > 5


@respx.mock
async def test_size_cap():
    respx.get("https://example.org/huge").mock(return_value=httpx.Response(200, content=b"x" * 2048))
    async with PoliteClient(rates={"web": 1000}) as c:
        c.max_bytes = 1000
        res = await c.get("https://example.org/huge")
    assert res.status == 413 and "exceeds" in res.error
