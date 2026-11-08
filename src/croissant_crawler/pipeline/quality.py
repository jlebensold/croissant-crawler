"""Quality rubric for Croissant metadata (0-100), transparent and versioned.

Each check contributes ``weight * fraction`` where fraction is in [0, 1]. The breakdown is
stored with every validation so scores are explainable in the UI and reproducible offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .extract import DatasetRecord

RUBRIC_VERSION = "q1"
BOILERPLATE = ("add a description", "no description", "todo", "[more information needed]", "dataset card")


@dataclass
class Check:
    id: str
    category: str
    label: str
    weight: float
    fraction: float

    @property
    def score(self) -> float:
        return round(self.weight * max(0.0, min(1.0, self.fraction)), 2)


def _ratio(n: int, d: int) -> float:
    return n / d if d else 0.0


def _description_quality(desc: str) -> float:
    d = desc.strip()
    if not d or any(b in d.lower()[:200] for b in BOILERPLATE) and len(d) < 300:
        return 0.0
    if len(d) >= 300:
        return 1.0
    if len(d) >= 100:
        return 0.7
    if len(d) >= 30:
        return 0.35
    return 0.1


def score(rec: DatasetRecord, n_errors: int, n_warnings: int) -> tuple[float, str, dict[str, Any]]:
    checks = [
        # Validity (25)
        Check("spec_valid", "validity", "Passes mlcroissant validation (no errors)", 20, 1.0 if n_errors == 0 else 0.0),
        Check("few_warnings", "validity", "Few validator warnings", 5, 1.0 if n_warnings == 0 else max(0.0, 1 - n_warnings / 6)),
        # Descriptive metadata (30)
        Check("name", "metadata", "Has a name", 2, 1.0 if rec.name and rec.name != "(untitled)" else 0.0),
        Check("description", "metadata", "Substantive description", 8, _description_quality(rec.description)),
        Check("license", "metadata", "Declares a recognised license", 6, 0.0 if rec.license in ("Unknown",) else (0.5 if rec.license == "Other" else 1.0)),
        Check("creator", "metadata", "Names a creator or publisher", 3, 1.0 if rec.creators or rec.publisher else 0.0),
        Check("url", "metadata", "Has a landing URL", 2, 1.0 if rec.url else 0.0),
        Check("dates", "metadata", "Has publication/modification dates", 2, 1.0 if rec.date_published or rec.date_modified else 0.0),
        Check("keywords", "metadata", "Has 3+ keywords", 2, min(1.0, len(rec.keywords) / 3)),
        Check("citation", "metadata", "Provides citeAs / citation", 3, 1.0 if rec.cite_as else 0.0),
        Check("version", "metadata", "Declares a version", 2, 1.0 if rec.version else 0.0),
        # Distribution (20)
        Check("has_files", "distribution", "Declares FileObjects / FileSets", 6, 1.0 if rec.num_files else 0.0),
        Check("file_urls", "distribution", "Files have content URLs", 4, _ratio(rec.files_with_url, rec.num_files)),
        Check("file_formats", "distribution", "Files declare encodingFormat", 4, _ratio(rec.files_with_format, rec.num_files)),
        Check("checksums", "distribution", "FileObjects have checksums", 4, _ratio(rec.files_with_checksum, rec.num_files)),
        Check("size", "distribution", "Declares content size", 2, 1.0 if rec.total_bytes else 0.0),
        # Structure (15)
        Check("record_sets", "structure", "Declares RecordSets", 5, 1.0 if rec.num_record_sets else 0.0),
        Check("typed_fields", "structure", "Fields declare dataType", 6, _ratio(rec.fields_with_type, rec.num_fields)),
        Check("field_docs", "structure", "Fields have descriptions", 4, _ratio(rec.fields_with_description, rec.num_fields)),
        # Responsible AI (10)
        Check("rai", "responsible_ai", "Responsible-AI properties (rai:*)", 10, min(1.0, len(rec.rai_properties) / 4)),
    ]
    total = round(sum(c.score for c in checks), 1)
    if n_errors:
        total = min(total, 49.0)  # invalid documents can never rank as good
    tier = tier_for(total, n_errors)
    cats: dict[str, dict[str, float]] = {}
    for c in checks:
        cat = cats.setdefault(c.category, {"score": 0.0, "max": 0.0})
        cat["score"] = round(cat["score"] + c.score, 2)
        cat["max"] += c.weight
    breakdown = {
        "rubric": RUBRIC_VERSION,
        "categories": cats,
        "checks": [
            {"id": c.id, "category": c.category, "label": c.label, "weight": c.weight, "score": c.score}
            for c in checks
        ],
    }
    return total, tier, breakdown


def tier_for(total: float, n_errors: int) -> str:
    if n_errors:
        return "F"
    if total >= 80:
        return "A"
    if total >= 65:
        return "B"
    if total >= 50:
        return "C"
    return "D"
