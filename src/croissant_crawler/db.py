"""Postgres access: a shared async connection pool and a tiny forward-only migration runner."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
if not MIGRATIONS_DIR.exists():  # installed as a wheel inside the container
    MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_pools: dict[int, AsyncConnectionPool] = {}


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


async def get_pool() -> AsyncConnectionPool:
    """One pool per event loop (Temporal activities, the web app and tests each run their own)."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    pool = _pools.get(key)
    if pool is None:
        pool = AsyncConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=False,
        )
        await pool.open(wait=True, timeout=30)
        _pools[key] = pool
    return pool


async def close_pool() -> None:
    loop = asyncio.get_running_loop()
    pool = _pools.pop(id(loop), None)
    if pool is not None:
        await pool.close()


@asynccontextmanager
async def connection():
    pool = await get_pool()
    async with pool.connection() as conn:
        yield conn


async def fetch_all(sql: str, params: Any = None) -> list[dict]:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: Any = None) -> dict | None:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: Any = None) -> int:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return cur.rowcount


async def migrate(database_url: str | None = None) -> list[str]:
    """Apply every migrations/*.sql file not yet recorded in schema_migrations."""
    url = database_url or get_settings().database_url
    applied: list[str] = []
    async with await AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        # Serialise concurrent migrators (worker + web start together).
        await conn.execute("SELECT pg_advisory_lock(727274)")
        try:
            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                log.info("applying migration %s", path.name)
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                    )
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(727274)")
    return applied
