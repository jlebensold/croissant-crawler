"""A polite async HTTP client.

* per-host token-bucket rate limiting (shared by every request in the process)
* honours ``Retry-After`` and the IETF ``RateLimit`` headers Hugging Face sends
* bounded retries with jittered exponential backoff on 429 / 5xx / network errors
* conditional requests (ETag / Last-Modified) and a hard response-size cap
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class TokenBucket:
    """Token bucket + circuit breaker for one upstream host group."""

    FAILURE_THRESHOLD = 8  # consecutive 5xx / network failures before the circuit opens
    OPEN_SECONDS = 600.0

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(rate, 0.01)
        self.capacity = burst if burst is not None else max(1.0, rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.blocked_until = 0.0
        self.consecutive_failures = 0
        self.open_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self.open_until

    def record(self, success: bool) -> None:
        if success:
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.FAILURE_THRESHOLD:
            self.open_until = time.monotonic() + self.OPEN_SECONDS
            log.warning("circuit opened for %.0fs after %d consecutive failures",
                        self.OPEN_SECONDS, self.consecutive_failures)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self.blocked_until:
                    await asyncio.sleep(self.blocked_until - now)
                    continue
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)

    def block_for(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


# Host -> source rate mapping. Unknown hosts get a conservative default.
HOST_RATE_KEYS = {
    "huggingface.co": "huggingface",
    "datasets-server.huggingface.co": "huggingface",
    "www.openml.org": "openml",
    "openml.org": "openml",
    "www.kaggle.com": "kaggle",
    "storage.googleapis.com": "kaggle",
}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    error: str | None = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class PoliteClient:
    def __init__(self, rates: dict[str, float] | None = None, max_retries: int = 4):
        s = get_settings()
        self.rates = rates or s.source_rates
        self.max_retries = max_retries
        self.max_bytes = s.max_document_bytes
        self._buckets: dict[str, TokenBucket] = {}
        headers = {"User-Agent": s.user_agent, "Accept": "application/ld+json, application/json;q=0.9, */*;q=0.5"}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(s.http_timeout_seconds, connect=10),
            follow_redirects=True,
            headers=headers,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
        self._auth: dict[str, dict[str, str]] = {}
        if s.hf_token:
            self._auth["huggingface.co"] = {"Authorization": f"Bearer {s.hf_token}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def bucket_for(self, url: str) -> TokenBucket:
        host = urlparse(url).hostname or ""
        key = HOST_RATE_KEYS.get(host)
        if key is None:
            key = "dataverse" if "dataverse" in host else host
        rate = self.rates.get(key, self.rates.get("web", 2.0))
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = TokenBucket(rate)
        return bucket

    def _apply_rate_headers(self, bucket: TokenBucket, resp: httpx.Response) -> None:
        # Hugging Face: `ratelimit: "api";r=499;t=21` (remaining requests, seconds to reset)
        value = resp.headers.get("ratelimit")
        if value:
            m_r = re.search(r"\br=(\d+)", value)
            m_t = re.search(r"\bt=(\d+)", value)
            if m_r and m_t and int(m_r.group(1)) <= 2:
                wait = int(m_t.group(1)) + 1
                log.warning("rate-limit budget exhausted for %s; pausing %ss", resp.url.host, wait)
                bucket.block_for(wait)

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        req_headers = dict(headers or {})
        host = urlparse(url).hostname or ""
        req_headers.update(self._auth.get(host, {}))
        if etag:
            req_headers["If-None-Match"] = etag
        if last_modified:
            req_headers["If-Modified-Since"] = last_modified
        bucket = self.bucket_for(url)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            if bucket.circuit_open:
                return FetchResult(url, url, 0, {}, b"", "circuit open: upstream unhealthy, backing off")
            await bucket.acquire()
            started = time.monotonic()
            try:
                async with self._client.stream("GET", url, params=params, headers=req_headers) as resp:
                    self._apply_rate_headers(bucket, resp)
                    bucket.record(resp.status_code < 500)
                    # A plain 500 is usually deterministic (converter bug); retry it only once.
                    max_attempts = 1 if resp.status_code == 500 else self.max_retries
                    if resp.status_code in RETRYABLE_STATUS and attempt < max_attempts:
                        wait = _retry_after(resp) or _backoff(attempt)
                        if resp.status_code == 429:
                            bucket.block_for(wait)
                        log.info("GET %s -> %s; retrying in %.1fs", url, resp.status_code, wait)
                        await asyncio.sleep(wait)
                        continue
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            return FetchResult(url, str(resp.url), 413, dict(resp.headers),
                                               b"", f"response exceeds {self.max_bytes} bytes")
                    return FetchResult(
                        url=url,
                        final_url=str(resp.url),
                        status=resp.status_code,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        content=bytes(body),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                bucket.record(False)
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    await asyncio.sleep(_backoff(attempt))
                    continue
        return FetchResult(url, url, 0, {}, b"", last_error or "request failed")


def _backoff(attempt: int) -> float:
    return min(60.0, (2**attempt) + random.random())


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return min(120.0, float(value))
    except ValueError:
        return None
