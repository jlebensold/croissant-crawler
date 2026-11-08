"""Normalise a Croissant document into a flat ``DatasetRecord`` used by the store, search and graph."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import jsonld as J
from .licenses import normalize_license

HF_TASKS = {
    "text-classification", "token-classification", "table-question-answering", "question-answering",
    "zero-shot-classification", "translation", "summarization", "feature-extraction",
    "text-generation", "text2text-generation", "fill-mask", "sentence-similarity", "text-to-speech",
    "text-to-audio", "automatic-speech-recognition", "audio-to-audio", "audio-classification",
    "voice-activity-detection", "depth-estimation", "image-classification", "object-detection",
    "image-segmentation", "text-to-image", "image-to-text", "image-to-image", "image-to-video",
    "unconditional-image-generation", "video-classification", "reinforcement-learning", "robotics",
    "tabular-classification", "tabular-regression", "tabular-to-text", "table-to-text",
    "multiple-choice", "text-retrieval", "time-series-forecasting", "text-to-video",
    "visual-question-answering", "document-question-answering", "zero-shot-image-classification",
    "graph-ml", "mask-generation", "zero-shot-object-detection", "text-to-3d", "image-to-3d",
    "image-feature-extraction", "video-text-to-text", "keypoint-detection", "image-text-to-text",
    "visual-document-retrieval", "any-to-any", "text-ranking", "audio-text-to-text",
}
MODALITY_WORDS = {
    "text": "text", "image": "image", "audio": "audio", "video": "video", "tabular": "tabular",
    "timeseries": "time-series", "time-series": "time-series", "geospatial": "geospatial",
    "3d": "3d", "document": "document",
}
SIZE_RE = re.compile(r"^(n<1k|n>1t|\d+(\.\d+)?[kmbt]?\s*-\s*\d+(\.\d+)?[kmbt]?)$", re.I)
ARXIV_RE = re.compile(r"(?:arxiv(?:\.org/(?:abs|pdf)/|:)\s*)(\d{4}\.\d{4,5})", re.I)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;}\]]+)", re.I)
SIZE_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
RAI_CORE = ("dataCollection", "dataBiases", "personalSensitiveInformation", "dataLimitations",
            "dataUseCases", "dataSocialImpact", "annotationProtocol", "dataPreprocessingProtocol",
            "dataManipulationProtocol", "dataReleaseMaintenancePlan", "annotationsPerItem",
            "annotatorDemographics", "machineAnnotationTools", "dataCollectionType",
            "dataCollectionRawData", "dataAnnotationProtocol", "dataAnnotationPlatform")


@dataclass
class DatasetRecord:
    name: str
    description: str = ""
    alternate_names: list[str] = field(default_factory=list)
    url: str | None = None
    same_as: list[str] = field(default_factory=list)
    license: str = "Unknown"
    license_raw: str | None = None
    creators: list[dict[str, Any]] = field(default_factory=list)
    publisher: str | None = None
    keywords: list[str] = field(default_factory=list)
    date_published: datetime | None = None
    date_modified: datetime | None = None
    version: str | None = None
    croissant_version: str | None = None
    cite_as: str | None = None
    in_language: list[str] = field(default_factory=list)
    is_live_dataset: bool = False
    # distribution
    num_files: int = 0
    num_file_objects: int = 0
    num_file_sets: int = 0
    files_with_url: int = 0
    files_with_format: int = 0
    files_with_checksum: int = 0
    encoding_formats: list[str] = field(default_factory=list)
    total_bytes: int | None = None
    # structure
    num_record_sets: int = 0
    num_fields: int = 0
    fields_with_type: int = 0
    fields_with_description: int = 0
    field_names: list[str] = field(default_factory=list)
    data_types: list[str] = field(default_factory=list)
    has_examples: bool = False
    # responsible AI
    rai_properties: list[str] = field(default_factory=list)
    # derived hints (deterministic, before any LLM)
    identifiers: dict[str, list[str]] = field(default_factory=dict)
    tasks: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=list)
    size_category: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("date_published", "date_modified"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


def parse_date(value: Any) -> datetime | None:
    s = J.text(value)
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}", s):
        s = f"{s}-01-01"
    # trim fractional seconds Python can't parse (e.g. ".21" is fine, ".1234567" is not)
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    for candidate in (s, s[:19], s[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        if 1900 <= dt.year <= 2100:
            return dt
    return None


def parse_size(value: Any) -> int | None:
    s = J.text(value)
    if not s:
        return None
    m = re.fullmatch(r"\s*([\d.,]+)\s*([kmgt]i?b|b|bytes?)?\s*", s.lower())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "b").replace("bytes", "b").replace("byte", "b")
    return int(num * SIZE_UNITS.get(unit, 1))


def _creator(node: Any) -> dict[str, Any] | None:
    if isinstance(node, str):
        return {"name": node.strip(), "type": "Unknown"} if node.strip() else None
    if not isinstance(node, dict):
        return None
    name = J.text(J.get(node, "name"))
    if not name:
        given, family = J.text(J.get(node, "givenName")), J.text(J.get(node, "familyName"))
        name = " ".join(p for p in (given, family) if p) or None
    if not name:
        return None
    ctype = (J.types(node) or ["Unknown"])[0]
    out = {"name": name, "type": ctype}
    url = J.text(J.get(node, "url"))
    if url:
        out["url"] = url
    return out


def _walk_fields(fields: list, acc: list[dict]) -> None:
    for f in fields:
        if not isinstance(f, dict):
            continue
        acc.append(f)
        _walk_fields(J.as_list(J.get(f, "subField")), acc)


def _parse_keyword_hints(keywords: list[str], rec: DatasetRecord) -> None:
    for kw in keywords:
        k = kw.strip().lower()
        if k in HF_TASKS and k not in rec.tasks:
            rec.tasks.append(k)
        if k in MODALITY_WORDS and MODALITY_WORDS[k] not in rec.modalities:
            rec.modalities.append(MODALITY_WORDS[k])
        if SIZE_RE.match(k.replace(" ", "")) or SIZE_RE.match(k):
            rec.size_category = kw.strip()


def _clean_description(desc: str) -> str:
    desc = desc.replace("\t", " ")
    desc = re.sub(r"[  ]+", " ", desc)
    desc = re.sub(r"\n\s*\n+", "\n\n", desc)
    return desc.strip()


def extract(doc: dict) -> DatasetRecord:
    name = J.text(J.get(doc, "name")) or J.text(J.get(doc, "alternateName")) or "(untitled)"
    rec = DatasetRecord(name=name[:500])
    rec.description = _clean_description(J.text(J.get(doc, "description")) or "")
    rec.alternate_names = J.texts(J.get(doc, "alternateName"))
    rec.url = J.text(J.get(doc, "url"))
    rec.same_as = J.texts(J.get(doc, "sameAs"))

    lic_parts = []
    for lic in J.as_list(J.get(doc, "license")):
        if isinstance(lic, dict):
            lic_parts += [p for p in (J.text(J.get(lic, "name")), J.text(J.get(lic, "url")), J.text(lic.get("@id"))) if p]
        elif J.text(lic):
            lic_parts.append(J.text(lic))
    rec.license_raw = " | ".join(lic_parts)[:500] or None
    rec.license = normalize_license(" ".join(lic_parts)) if lic_parts else "Unknown"

    for key in ("creator", "author"):
        for node in J.as_list(J.get(doc, key)):
            c = _creator(node)
            if c and c not in rec.creators:
                rec.creators.append(c)
    pub = J.as_list(J.get(doc, "publisher"))
    rec.publisher = (_creator(pub[0]) or {}).get("name") if pub else None

    kws: list[str] = []
    for kw in J.texts(J.get(doc, "keywords")):
        parts = [p.strip() for p in kw.split(",")] if ("," in kw and len(J.as_list(J.get(doc, "keywords"))) == 1) else [kw]
        kws += [p for p in parts if p]
    seen: set[str] = set()
    rec.keywords = [k for k in kws if not (k.lower() in seen or seen.add(k.lower()))][:100]

    rec.date_published = parse_date(J.get(doc, "datePublished")) or parse_date(J.get(doc, "dateCreated"))
    rec.date_modified = parse_date(J.get(doc, "dateModified"))
    rec.version = J.text(J.get(doc, "version"))
    rec.croissant_version = J.croissant_version(doc)
    rec.cite_as = J.text(J.get(doc, "citeAs")) or J.text(J.get(doc, "citation"))
    rec.in_language = J.texts(J.get(doc, "inLanguage"))
    rec.is_live_dataset = bool(J.get(doc, "isLiveDataset") in (True, "true", "True"))

    # --- distribution ---------------------------------------------------------------
    formats: list[str] = []
    total = 0
    any_size = False
    for item in J.as_list(J.get(doc, "distribution")):
        if not isinstance(item, dict):
            continue
        t = J.types(item)
        rec.num_files += 1
        if "FileSet" in t:
            rec.num_file_sets += 1
        else:
            rec.num_file_objects += 1
        if J.text(J.get(item, "contentUrl")) or J.get(item, "includes") or J.get(item, "containedIn"):
            rec.files_with_url += 1
        fmt = J.text(J.get(item, "encodingFormat"))
        if fmt:
            rec.files_with_format += 1
            if fmt not in formats:
                formats.append(fmt)
        if J.get(item, "sha256") or J.get(item, "md5") or "FileSet" in t:
            rec.files_with_checksum += 1  # FileSets have no checksum by definition
        size = parse_size(J.get(item, "contentSize"))
        if size is not None:
            any_size = True
            total += size
    rec.encoding_formats = formats[:30]
    rec.total_bytes = total if any_size else None

    # --- record sets ----------------------------------------------------------------
    fields: list[dict] = []
    data_types: set[str] = set()
    for rs in J.as_list(J.get(doc, "recordSet")):
        if not isinstance(rs, dict):
            continue
        rec.num_record_sets += 1
        if J.get(rs, "examples") or J.get(rs, "data"):
            rec.has_examples = True
        _walk_fields(J.as_list(J.get(rs, "field")), fields)
    rec.num_fields = len(fields)
    for f in fields:
        dts = J.texts(J.get(f, "dataType"))
        if dts:
            rec.fields_with_type += 1
            data_types.update(d.split(":")[-1].split("/")[-1] for d in dts)
        if J.text(J.get(f, "description")):
            rec.fields_with_description += 1
        fname = J.text(J.get(f, "name")) or J.text(f.get("@id"))
        if fname and len(rec.field_names) < 200:
            rec.field_names.append(fname)
    rec.data_types = sorted(data_types)

    # --- responsible AI -------------------------------------------------------------
    rai = []
    for key, value in doc.items():
        short = key.split(":")[-1].split("/")[-1]
        if (key.startswith(J.RAI_PREFIXES) or short in RAI_CORE) and J.text(value):
            rai.append(short)
    rec.rai_properties = sorted(set(rai))

    # --- identifiers & hints ----------------------------------------------------------
    haystack = " ".join([rec.cite_as or "", rec.url or "", *rec.same_as, *rec.keywords, rec.description[:5000]])
    arxiv = sorted(set(ARXIV_RE.findall(haystack)))
    dois = sorted({d.rstrip(".)") for d in DOI_RE.findall(haystack)})
    rec.identifiers = {k: v for k, v in (("arxiv", arxiv), ("doi", dois)) if v}
    _parse_keyword_hints(rec.keywords, rec)
    return rec
