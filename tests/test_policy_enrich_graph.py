from croissant_crawler.pipeline.enrich import SCHEMA, _clean, _ground, build_digest
from croissant_crawler.pipeline.graph import build, name_key
from croissant_crawler.pipeline.store import next_interval_hours


def test_adaptive_recrawl_policy():
    assert next_interval_hours(96, "changed", 0, 24, 720) == 24
    assert next_interval_hours(24, "unchanged", 0, 24, 720) == 48
    assert next_interval_hours(600, "unchanged", 0, 24, 720) == 720
    assert next_interval_hours(24, "error", 1, 24, 720) == 1
    assert next_interval_hours(24, "error", 4, 24, 720) == 8
    assert next_interval_hours(24, "not_found", 3, 24, 720) == 720


def test_enrichment_schema_is_strict():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])


def test_clean_and_ground_drop_hallucinations():
    raw = {"summary": " x ", "tasks": ["math", "not-a-task"], "modalities": ["text"], "domains": ["mathematics"],
           "languages": ["en", "english"], "organizations": ["OpenAI", "Meta AI"], "derived_from": ["MNIST", "ImageNet"],
           "papers": ["2110.14168", "2015.00001"], "quality_flags": ["benchmark", "bogus"],
           "is_test_or_placeholder": False, "documentation_quality": 9}
    out = _ground(_clean(raw), "gsm8k by openai, see arxiv:2110.14168; derived from mnist")
    assert out["tasks"] == ["math"]
    assert out["languages"] == ["en"]
    assert out["papers"] == ["2110.14168"]
    assert out["organizations"] == ["OpenAI"]
    assert out["derived_from"] == ["MNIST"]
    assert out["quality_flags"] == ["benchmark"]
    assert out["documentation_quality"] == 5


def test_digest_is_bounded():
    row = {"source_id": "huggingface", "external_id": "a/b", "name": "b", "description": "word " * 5000,
           "keywords": ["k"] * 100, "creators": [], "extra": {}, "source_meta": {"downloads": 5}}
    d = build_digest(row)
    assert len(d) < 6000 and "truncated" in d


def _row(i, name, source, **enr):
    return {"link_id": i, "name": name, "source_id": source, "external_id": f"x/{name}", "quality_tier": "B",
            "quality_score": 70.0, "popularity": 1.0, "license": "MIT", "creators": [{"name": "Org Inc", "type": "Organization"}],
            "identifiers": {"arxiv": ["2110.14168"]}, "extra": {"tasks": ["text-generation"], "modalities": ["text"]},
            "enrichment": enr or None}


def test_graph_build_links_entities():
    rows = [
        _row(1, "MNIST", "huggingface"),
        _row(2, "mnist", "kaggle"),
        _row(3, "Fashion thing", "huggingface", derived_from=["MNIST", "Unknown Corpus"], domains=["general"]),
        _row(4, "train", "openml"),
        _row(5, "train", "kaggle"),
    ]
    g = build(rows, [{"link_id": 1, "other_id": 3, "score": 0.8}])
    types = {(s, d, t) for (s, d, t) in g.edges}
    assert ("dataset:1", "dataset:2", "SAME_AS") in types
    assert ("dataset:4", "dataset:5", "SAME_AS") not in types  # generic names never merge
    assert ("dataset:3", "dataset:1", "DERIVED_FROM") in types
    assert any(d.startswith("external_dataset:") for (_, d, t) in g.edges if t == "DERIVED_FROM")
    assert ("dataset:1", "dataset:3", "SIMILAR_TO") in types
    assert ("dataset:1", "license:MIT", "LICENSED_UNDER") in types
    assert ("dataset:1", "paper:arxiv:2110.14168", "CITES") in types
    assert "org:org-inc" in g.nodes


def test_name_key():
    assert name_key("ylecun/mnist") == name_key("MNIST dataset") == "mnist"
