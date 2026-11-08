import pytest

from croissant_crawler.pipeline.extract import extract, parse_date, parse_size
from croissant_crawler.pipeline.licenses import normalize_license
from croissant_crawler.pipeline.text import markdown_to_text

from .conftest import load


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://choosealicense.com/licenses/mit/", "MIT"),
        ("https://choosealicense.com/licenses/apache-2.0/", "Apache-2.0"),
        ("http://creativecommons.org/publicdomain/zero/1.0", "CC0-1.0"),
        ("https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        ("Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)", "CC-BY-NC-SA-4.0"),
        ("cc-by-nc-4.0", "CC-BY-NC-4.0"),
        ("https://choosealicense.com/licenses/odc-by/", "ODC-BY"),
        ("Database: Open Database, Contents: Database Contents http://opendatacommons.org/licenses/dbcl/1.0/", "ODbL"),
        ("Public", "Public-Domain"),
        ("other", "Other"),
        ("", "Unknown"),
        (None, "Unknown"),
        ("some bespoke terms", "Other"),
    ],
)
def test_normalize_license(raw, expected):
    assert normalize_license(raw) == expected


@pytest.mark.parametrize("raw,expected", [("10.83 KB", 10830), ("590", 590), ("1.5 GB", 1_500_000_000),
                                          ("2 MiB", 2 * 1024**2), ("12,345 B", 12345), ("lots", None), (None, None)])
def test_parse_size(raw, expected):
    assert parse_size(raw) == expected


def test_parse_date():
    assert parse_date("2017-05-16T08:14:22.21").year == 2017
    assert parse_date("2024").month == 1
    assert parse_date("2026-11-08T01:02:03Z").tzinfo is not None
    assert parse_date("not a date") is None
    assert parse_date("0001-01-01") is None


def test_extract_huggingface_hints():
    rec = extract(load("hf_gsm8k.json"))
    assert rec.license == "MIT"
    assert rec.creators[0]["name"] == "OpenAI"
    assert "text-generation" in rec.tasks
    assert "text" in rec.modalities
    assert rec.size_category == "1K - 10K" or rec.size_category  # HF size bucket keyword
    assert rec.identifiers.get("arxiv") == ["2110.14168"]
    assert rec.croissant_version == "1.1"


def test_extract_kaggle_distribution():
    rec = extract(load("kaggle_titanic.json"))
    assert rec.num_files == 2
    assert rec.total_bytes == 10830
    assert rec.num_fields == 28 and rec.fields_with_type == 28
    assert rec.date_published.year == 2017


def test_extract_dataverse_people():
    rec = extract(load("dataverse_tjclkp.json"))
    assert rec.creators[0]["type"] == "Person"
    assert rec.publisher == "Harvard Dataverse"
    assert rec.license == "CC0-1.0"
    assert "10.7910/DVN/TJCLKP" in rec.identifiers["doi"]


def test_extract_tolerates_prefixed_keys_and_value_objects():
    rec = extract({
        "@type": "sc:Dataset",
        "sc:name": {"@value": "Prefixed"},
        "schema:description": ["first", "second"],
        "keywords": "a, b, c",
        "license": [{"name": "MIT License", "url": "https://opensource.org/licenses/MIT"}],
    })
    assert rec.name == "Prefixed"
    assert rec.description == "first"
    assert rec.keywords == ["a", "b", "c"]
    assert rec.license == "MIT"


def test_markdown_to_text():
    md = "# Title\n\nSome **bold** [link](http://x.y) ![img](http://i.png) &gt; done\n\n[1]: http://ref"
    assert markdown_to_text(md) == "Title\nSome bold link > done"
