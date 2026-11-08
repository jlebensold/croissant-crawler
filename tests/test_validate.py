import copy

import pytest

from croissant_crawler.pipeline import jsonld
from croissant_crawler.pipeline.validate import validate

from .conftest import load


@pytest.mark.parametrize(
    "name,status,version,tier",
    [
        ("hf_mnist.json", "valid_with_warnings", "1.1", "B"),
        ("hf_gsm8k.json", "valid_with_warnings", "1.1", "B"),
        ("openml_31.json", "valid", "1.0", "A"),
        ("kaggle_titanic.json", "valid_with_warnings", "1.0", "B"),
        ("dataverse_tjclkp.json", "valid", "1.0", "A"),
        ("dataverse_invalid.json", "invalid", "1.0", "F"),
    ],
)
def test_real_documents(name, status, version, tier):
    report, rec = validate(load(name))
    assert report.status == status
    assert report.croissant_version == version
    assert report.quality_tier == tier
    assert rec is not None and rec.name


def test_invalid_documents_are_capped_below_50():
    report, _ = validate(load("dataverse_invalid.json"))
    assert report.errors and report.quality_score <= 49
    # ValidationError raised inside mlcroissant is unpacked into individual messages
    assert any("whitespace" in e for e in report.errors)


def test_not_croissant():
    report, rec = validate({"@context": "https://schema.org/", "@type": "Dataset", "name": "plain schema.org"})
    assert report.status == "not_croissant"
    assert rec is None


def test_removing_license_lowers_score():
    doc = load("openml_31.json")
    base, _ = validate(doc)
    stripped = copy.deepcopy(doc)
    stripped.pop("license")
    worse, _ = validate(stripped)
    assert worse.quality_score < base.quality_score


def test_breakdown_is_explainable():
    report, _ = validate(load("hf_mnist.json"))
    cats = report.quality_breakdown["categories"]
    assert set(cats) == {"validity", "metadata", "distribution", "structure", "responsible_ai"}
    assert sum(c["max"] for c in cats.values()) == 100
    assert round(sum(c["score"] for c in cats.values()), 1) == report.quality_score


def test_parse_document_variants():
    doc, err = jsonld.parse_document(b"", "application/json")
    assert doc is None and "empty" in err
    doc, err = jsonld.parse_document(b"{not json", "application/json")
    assert doc is None and "invalid JSON" in err
    doc, err = jsonld.parse_document(b'[{"a": 1}]', "application/json")
    assert doc == {"a": 1}


def test_jsonld_embedded_in_html():
    import orjson

    croissant = load("hf_mnist.json")
    page = (b"<html><head><script type='application/ld+json'>{\"@type\": \"WebSite\"}</script>"
            b"<script type=\"application/ld+json\">" + orjson.dumps(croissant) + b"</script></head></html>")
    doc, err = jsonld.parse_document(page, "text/html; charset=utf-8")
    assert err is None and doc["name"] == croissant["name"]


def test_canonical_hash_ignores_key_order():
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert jsonld.sha256_of(a) == jsonld.sha256_of(b)


def test_nul_characters_are_stripped():
    doc, err = jsonld.parse_document(b'{"name": "a\\u0000b", "k\\u0000": ["PK\\u0003\\u0000"]}', "application/json")
    assert err is None
    assert doc == {"name": "ab", "k": ["PK\u0003"]}
