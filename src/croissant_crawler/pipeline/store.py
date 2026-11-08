"""Data-access functions for the crawl pipeline (plain SQL, no ORM)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .. import db
from ..db import jsonb
from ..sources import DiscoveredLink, all_source_ids, get_source
from .extract import DatasetRecord
from .validate import ValidationReport

MIN_REVISIT_SECONDS = 300


async def ensure_sources() -> None:
    for sid in all_source_ids():
        src = get_source(sid)
        await db.execute(
            "INSERT INTO sources (id, name, base_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, base_url = EXCLUDED.base_url",
            (src.id, src.name, src.base_url),
        )


async def get_cursor(source_id: str) -> dict | None:
    row = await db.fetch_one("SELECT discovery_cursor FROM sources WHERE id = %s", (source_id,))
    return row["discovery_cursor"] if row else None


async def set_cursor(source_id: str, cursor: dict | None, *, started: bool = False, finished: bool = False) -> None:
    sets = ["discovery_cursor = %s"]
    if started:
        sets.append("last_discovery_started_at = now()")
    if finished:
        sets.append("last_discovery_finished_at = now()")
    await db.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id = %s", (jsonb(cursor), source_id))


async def upsert_links(source_id: str, links: list[DiscoveredLink]) -> int:
    """Insert new links; refresh listing metadata on known ones.

    If the upstream ``source_version`` moved since our last fetch, the link becomes due now.
    Returns the number of *new* links.
    """
    if not links:
        return 0
    new = 0
    async with db.connection() as conn:
        async with conn.transaction():
            for link in links:
                cur = await conn.execute(
                    """
                    INSERT INTO links (source_id, external_id, croissant_url, landing_url, source_meta,
                                       source_version, popularity, priority)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_id, external_id) DO UPDATE SET
                        last_seen_at = now(),
                        source_meta = EXCLUDED.source_meta,
                        popularity = EXCLUDED.popularity,
                        priority = EXCLUDED.priority,
                        source_version = EXCLUDED.source_version,
                        next_fetch_at = CASE
                            WHEN EXCLUDED.source_version IS NOT NULL
                             AND EXCLUDED.source_version <> ''
                             AND links.fetched_source_version IS DISTINCT FROM EXCLUDED.source_version
                             AND links.status IN ('ok', 'not_modified', 'invalid')
                            THEN LEAST(links.next_fetch_at, now())
                            ELSE links.next_fetch_at END
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (
                        source_id,
                        link.external_id,
                        link.croissant_url,
                        link.landing_url,
                        jsonb(link.meta),
                        link.source_version or None,
                        link.popularity,
                        link.priority,
                    ),
                )
                row = await cur.fetchone()
                if row and row["inserted"]:
                    new += 1
    return new


async def add_link(source_id: str, link: DiscoveredLink) -> int:
    await upsert_links(source_id, [link])
    row = await db.fetch_one(
        "SELECT id FROM links WHERE source_id = %s AND external_id = %s", (source_id, link.external_id)
    )
    return int(row["id"])


async def lease_due_links(source_id: str, limit: int, lease_minutes: int = 15) -> list[int]:
    rows = await db.fetch_all(
        """
        UPDATE links SET lease_until = now() + make_interval(mins => %s)
        WHERE id IN (
            SELECT id FROM links
            WHERE source_id = %s AND next_fetch_at <= now()
              AND (lease_until IS NULL OR lease_until < now())
              AND status <> 'gone'
            ORDER BY priority DESC, next_fetch_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED)
        RETURNING id
        """,
        (lease_minutes, source_id, limit),
    )
    return [r["id"] for r in rows]


async def count_due(source_id: str) -> int:
    row = await db.fetch_one(
        "SELECT count(*) AS n FROM links WHERE source_id = %s AND next_fetch_at <= now() "
        "AND (lease_until IS NULL OR lease_until < now()) AND status <> 'gone'",
        (source_id,),
    )
    return int(row["n"]) if row else 0


async def get_link(link_id: int) -> dict | None:
    return await db.fetch_one("SELECT * FROM links WHERE id = %s", (link_id,))


def next_interval_hours(prev_hours: float, outcome: str, consecutive_errors: int, min_h: float, max_h: float) -> float:
    """Adaptive revisit policy.

    * changed      -> come back soon (min interval)
    * unchanged    -> double the interval up to the max
    * not_found    -> retry a few times, then park at the max interval
    * error        -> exponential backoff starting at 1h
    """
    if outcome == "changed":
        return min_h
    if outcome in ("unchanged", "not_modified", "skipped"):
        return min(max(prev_hours, min_h) * 2, max_h)
    if outcome in ("not_found", "forbidden", "invalid"):
        return max_h if consecutive_errors >= 3 else min(min_h * (2**consecutive_errors), max_h)
    return min(2.0 ** max(consecutive_errors - 1, 0), max_h)


async def record_fetch(
    link_id: int,
    *,
    status: str,
    outcome: str,
    http_status: int | None,
    recrawl_hours: float,
    error: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    final_url: str | None = None,
    document_id: int | None = None,
    changed: bool = False,
    fetched_source_version: str | None = None,
    count_fetch: bool = True,
) -> None:
    is_error = outcome in ("error", "not_found", "forbidden", "invalid")
    await db.execute(
        """
        UPDATE links SET
            status = %(status)s,
            http_status = COALESCE(%(http_status)s, http_status),
            last_error = %(error)s,
            etag = COALESCE(%(etag)s, etag),
            last_modified = COALESCE(%(last_modified)s, last_modified),
            final_url = COALESCE(%(final_url)s, final_url),
            last_fetched_at = CASE WHEN %(count_fetch)s THEN now() ELSE last_fetched_at END,
            fetch_count = fetch_count + CASE WHEN %(count_fetch)s THEN 1 ELSE 0 END,
            last_changed_at = CASE WHEN %(changed)s THEN now() ELSE last_changed_at END,
            current_document_id = COALESCE(%(document_id)s, current_document_id),
            fetched_source_version = COALESCE(%(fsv)s, fetched_source_version),
            consecutive_errors = CASE WHEN %(is_error)s THEN consecutive_errors + 1 ELSE 0 END,
            recrawl_hours = %(recrawl_hours)s,
            next_fetch_at = now() + make_interval(secs => %(recrawl_secs)s),
            lease_until = NULL
        WHERE id = %(id)s
        """,
        {
            "id": link_id,
            "status": status,
            "http_status": http_status,
            "error": (error or None) and error[:2000],
            "etag": etag,
            "last_modified": last_modified,
            "final_url": final_url,
            "count_fetch": count_fetch,
            "changed": changed,
            "document_id": document_id,
            "fsv": fetched_source_version,
            "is_error": is_error,
            "recrawl_hours": recrawl_hours,
            # floor: never revisit within the same run, whatever the configured minimum
            "recrawl_secs": max(recrawl_hours * 3600, MIN_REVISIT_SECONDS),
        },
    )


async def defer_link(link_id: int, minutes: int = 15) -> None:
    await db.execute(
        "UPDATE links SET lease_until = NULL, next_fetch_at = now() + make_interval(mins => %s) WHERE id = %s",
        (minutes, link_id),
    )


async def insert_document(link_id: int, sha256: str, size: int, content: dict) -> tuple[int, bool]:
    row = await db.fetch_one(
        "INSERT INTO documents (link_id, sha256, size_bytes, content) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (link_id, sha256) DO NOTHING RETURNING id",
        (link_id, sha256, size, jsonb(content)),
    )
    if row:
        return int(row["id"]), True
    row = await db.fetch_one(
        "SELECT id FROM documents WHERE link_id = %s AND sha256 = %s", (link_id, sha256)
    )
    return int(row["id"]), False


async def get_document(document_id: int) -> dict | None:
    return await db.fetch_one(
        "SELECT d.*, l.source_id FROM documents d JOIN links l ON l.id = d.link_id WHERE d.id = %s",
        (document_id,),
    )


async def save_validation(document_id: int, report: ValidationReport) -> None:
    await db.execute(
        """
        INSERT INTO validations (document_id, validator_version, status, croissant_version, errors,
                                 warnings, quality_score, quality_tier, quality_breakdown, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (document_id) DO UPDATE SET
            validator_version = EXCLUDED.validator_version, status = EXCLUDED.status,
            croissant_version = EXCLUDED.croissant_version, errors = EXCLUDED.errors,
            warnings = EXCLUDED.warnings, quality_score = EXCLUDED.quality_score,
            quality_tier = EXCLUDED.quality_tier, quality_breakdown = EXCLUDED.quality_breakdown,
            duration_ms = EXCLUDED.duration_ms, validated_at = now()
        """,
        (
            document_id,
            report.validator_version,
            report.status,
            report.croissant_version,
            jsonb(report.errors),
            jsonb(report.warnings),
            report.quality_score,
            report.quality_tier,
            jsonb(report.quality_breakdown),
            report.duration_ms,
        ),
    )


async def upsert_dataset(
    link_id: int, document_id: int, source_id: str, rec: DatasetRecord, report: ValidationReport
) -> None:
    extra = rec.to_json()
    for k in ("name", "description", "url", "license", "license_raw", "creators", "keywords",
              "date_published", "date_modified", "version", "croissant_version", "encoding_formats",
              "num_files", "num_record_sets", "num_fields", "total_bytes", "identifiers"):
        extra.pop(k, None)
    await db.execute(
        """
        INSERT INTO datasets (link_id, document_id, source_id, name, description, url, license, license_raw,
            creators, keywords, date_published, date_modified, version, croissant_version, encoding_formats,
            num_files, num_record_sets, num_fields, total_bytes, identifiers, extra, validation_status,
            quality_score, quality_tier, updated_at, indexed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), NULL)
        ON CONFLICT (link_id) DO UPDATE SET
            document_id = EXCLUDED.document_id, name = EXCLUDED.name, description = EXCLUDED.description,
            url = EXCLUDED.url, license = EXCLUDED.license, license_raw = EXCLUDED.license_raw,
            creators = EXCLUDED.creators, keywords = EXCLUDED.keywords,
            date_published = EXCLUDED.date_published, date_modified = EXCLUDED.date_modified,
            version = EXCLUDED.version, croissant_version = EXCLUDED.croissant_version,
            encoding_formats = EXCLUDED.encoding_formats, num_files = EXCLUDED.num_files,
            num_record_sets = EXCLUDED.num_record_sets, num_fields = EXCLUDED.num_fields,
            total_bytes = EXCLUDED.total_bytes, identifiers = EXCLUDED.identifiers, extra = EXCLUDED.extra,
            validation_status = EXCLUDED.validation_status, quality_score = EXCLUDED.quality_score,
            quality_tier = EXCLUDED.quality_tier, updated_at = now(), indexed_at = NULL
        """,
        (
            link_id, document_id, source_id, rec.name, rec.description[:20000], rec.url, rec.license,
            rec.license_raw, jsonb(rec.creators), rec.keywords, rec.date_published, rec.date_modified,
            rec.version, rec.croissant_version, rec.encoding_formats, rec.num_files, rec.num_record_sets,
            rec.num_fields, rec.total_bytes, jsonb(rec.identifiers), jsonb(extra), report.status,
            report.quality_score, report.quality_tier,
        ),
    )


async def log_event(kind: str, *, source_id: str | None = None, link_id: int | None = None, **detail: Any) -> None:
    await db.execute(
        "INSERT INTO crawl_events (source_id, link_id, kind, detail) VALUES (%s, %s, %s, %s)",
        (source_id, link_id, kind, jsonb(detail)),
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


def hours(td: timedelta) -> float:
    return td.total_seconds() / 3600
