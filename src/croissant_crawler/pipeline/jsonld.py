"""Helpers for reading Croissant JSON-LD robustly.

Real-world Croissant files vary: keys may be compacted (``name``) or prefixed (``sc:name``),
values may be strings, lists, ``{"@value": ...}`` objects or nested nodes. These helpers
normalise access without a full JSON-LD expansion (which is slow and needs the network).
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any

import orjson

SCHEMA_PREFIXES = ("", "sc:", "schema:", "https://schema.org/", "http://schema.org/")
CR_PREFIXES = ("", "cr:", "http://mlcommons.org/croissant/")
RAI_PREFIXES = ("rai:", "http://mlcommons.org/croissant/RAI/")
CROISSANT_NS = "http://mlcommons.org/croissant/"


def get(node: Any, key: str, prefixes: tuple[str, ...] = SCHEMA_PREFIXES + CR_PREFIXES) -> Any:
    if not isinstance(node, dict):
        return None
    for p in prefixes:
        if (p + key) in node:
            return node[p + key]
    return None


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def text(value: Any) -> str | None:
    """Collapse a JSON-LD value (string, number, @value object, list) to a single string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for k in ("@value", "name", "sc:name", "@id", "url"):
            if k in value:
                return text(value[k])
        return None
    if isinstance(value, list):
        for v in value:
            t = text(v)
            if t:
                return t
    return None


def texts(value: Any) -> list[str]:
    out = []
    for v in as_list(value):
        t = text(v)
        if t:
            out.append(t)
    return out


def types(node: Any) -> list[str]:
    return [t.split(":")[-1].split("/")[-1] for t in texts(node.get("@type")) ] if isinstance(node, dict) else []


def conforms_to(doc: dict) -> str | None:
    for v in texts(get(doc, "conformsTo", ("", "dct:", "http://purl.org/dc/terms/"))):
        if "mlcommons.org/croissant" in v:
            return v
    return None


def croissant_version(doc: dict) -> str | None:
    ct = conforms_to(doc)
    if not ct:
        return None
    m = re.search(r"croissant/(\d+\.\d+)", ct)
    return m.group(1) if m else ct


def looks_like_croissant(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    if conforms_to(doc):
        return True
    ctx = orjson.dumps(doc.get("@context", {})).decode()
    return CROISSANT_NS in ctx and ("Dataset" in types(doc))


def sanitize(value: Any) -> Any:
    """Drop NUL characters (seen in the wild: ZIP bytes used as Kaggle column names).

    Postgres JSONB cannot store U+0000; we sanitize before hashing so the content hash always
    matches what is stored.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {sanitize(k): sanitize(v) for k, v in value.items()}
    return value


def canonical_bytes(doc: Any) -> bytes:
    return orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)


def sha256_of(doc: Any) -> str:
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


_JSONLD_SCRIPT = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)


def extract_from_html(body: str) -> dict | None:
    """Return the first Croissant JSON-LD block embedded in an HTML page, if any."""
    for raw in _JSONLD_SCRIPT.findall(body):
        try:
            data = orjson.loads(html.unescape(raw.strip()))
        except orjson.JSONDecodeError:
            continue
        for candidate in as_list(data.get("@graph") if isinstance(data, dict) and "@graph" in data else data):
            if looks_like_croissant(candidate):
                return candidate
    return None


def parse_document(content: bytes, content_type: str = "") -> tuple[dict | None, str | None]:
    """Parse a fetched body into a JSON object. Returns (doc, error)."""
    if not content.strip():
        return None, "empty response body"
    stripped = content.lstrip()[:1]
    if stripped in (b"{", b"["):
        try:
            data = orjson.loads(content)
        except orjson.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), None)
        if not isinstance(data, dict):
            return None, "JSON root is not an object"
        return sanitize(data), None
    if "html" in content_type or stripped == b"<":
        doc = extract_from_html(content.decode("utf-8", "replace"))
        if doc is None:
            return None, "HTML page without embedded Croissant JSON-LD"
        return sanitize(doc), None
    return None, f"unsupported content type {content_type or 'unknown'}"
