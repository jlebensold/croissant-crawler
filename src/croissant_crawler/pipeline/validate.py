"""Tiered validation of a fetched Croissant document.

L0 parse      -> JSON object (or JSON-LD embedded in HTML)           [done by jsonld.parse_document]
L1 shape      -> looks like Croissant (context / conformsTo / Dataset type)
L2 spec       -> mlcroissant structural validation (errors + warnings), no data download
L3 quality    -> transparent rubric score 0-100 and tier A-F
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import jsonld as J
from .extract import DatasetRecord, extract
from .quality import RUBRIC_VERSION, score

log = logging.getLogger(__name__)
MAX_MESSAGES = 50


def _mlcroissant_version() -> str:
    try:
        from importlib.metadata import version

        return version("mlcroissant")
    except Exception:  # pragma: no cover
        return "unknown"


VALIDATOR_VERSION = f"mlc-{_mlcroissant_version()}+{RUBRIC_VERSION}"


@dataclass
class ValidationReport:
    status: str  # valid | valid_with_warnings | invalid | not_croissant
    croissant_version: str | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quality_score: float = 0.0
    quality_tier: str = "F"
    quality_breakdown: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    validator_version: str = VALIDATOR_VERSION

    @property
    def is_valid(self) -> bool:
        return self.status in ("valid", "valid_with_warnings")


def run_mlcroissant(doc: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) from mlcroissant without raising."""
    import mlcroissant as mlc
    from mlcroissant._src.core.context import Context

    logging.getLogger("absl").setLevel(logging.ERROR)
    ctx = Context()
    try:
        mlc.Metadata.from_json(ctx=ctx, json_=doc)
    except mlc.ValidationError as exc:
        # Some checks raise instead of recording; unpack the "  -  <message>" report lines.
        lines = [ln.strip()[1:].strip() for ln in str(exc).splitlines() if ln.strip().startswith("-")]
        errs = sorted(set(ctx.issues.errors) | set(lines or [str(exc)[:500]]))
        return errs, sorted(ctx.issues.warnings)
    except Exception as exc:  # mlcroissant can crash on malformed graphs
        errs = sorted(ctx.issues.errors)
        return errs + [f"mlcroissant crashed: {type(exc).__name__}: {str(exc)[:300]}"], sorted(ctx.issues.warnings)
    return sorted(ctx.issues.errors), sorted(ctx.issues.warnings)


def validate(doc: dict) -> tuple[ValidationReport, DatasetRecord | None]:
    started = time.monotonic()
    version = J.croissant_version(doc)
    if not J.looks_like_croissant(doc):
        report = ValidationReport(
            status="not_croissant",
            croissant_version=version,
            errors=["document does not declare conformsTo a Croissant spec nor use the Croissant context"],
        )
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return report, None
    errors, warnings = run_mlcroissant(doc)
    rec = extract(doc)
    total, tier, breakdown = score(rec, len(errors), len(warnings))
    status = "invalid" if errors else ("valid_with_warnings" if warnings else "valid")
    report = ValidationReport(
        status=status,
        croissant_version=version,
        errors=errors[:MAX_MESSAGES],
        warnings=warnings[:MAX_MESSAGES],
        quality_score=total,
        quality_tier=tier,
        quality_breakdown=breakdown,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return report, rec
