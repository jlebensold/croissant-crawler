from starlette.datastructures import QueryParams

from croissant_crawler.web.search_service import (
    END,
    START,
    SearchRequest,
    build_params,
    shape_results,
)


def test_build_params_filters_and_hybrid():
    req = SearchRequest.from_query(QueryParams("q=speech&source=huggingface&source=kaggle&license=CC-BY-4.0"))
    p = build_params(req, semantic_enabled=True)
    assert "source:=[`huggingface`,`kaggle`]" in p["filter_by"]
    assert "license:=[`CC-BY-4.0`]" in p["filter_by"]
    assert "is_placeholder:=false" in p["filter_by"]
    assert p["query_by"].endswith(",embedding") and "vector_query" in p
    assert p["sort_by"].startswith("_text_match(buckets:") and p["sort_by"].endswith("rank_score:desc")


def test_wildcard_sorts_by_rank_and_skips_vectors():
    p = build_params(SearchRequest.from_query(QueryParams("")), semantic_enabled=True)
    assert p["q"] == "*" and p["sort_by"] == "rank_score:desc" and "vector_query" not in p


def test_semantic_off():
    p = build_params(SearchRequest.from_query(QueryParams("q=x&semantic=0")), semantic_enabled=True)
    assert "embedding" not in p["query_by"]


def test_shape_results_escapes_html_but_keeps_marks():
    raw = {"found": 1, "hits": [{"document": {"id": "1", "name": "<b>x</b>", "description": "d"},
                                 "highlights": [{"field": "description", "snippet": f"<script>{START}hit{END}"}]}],
           "facet_counts": [{"field_name": "source", "counts": [{"value": "kaggle", "count": 1}]}]}
    req = SearchRequest.from_query(QueryParams("q=hit&source=openml"))
    out = shape_results(raw, req)
    hit = out["hits"][0]
    assert hit["snippet_html"] == "&lt;script&gt;<mark>hit</mark>"
    assert hit["name_html"] == "&lt;b&gt;x&lt;/b&gt;"
    values = {v["value"]: v for v in out["facets"][0]["values"]}
    assert values["openml"]["selected"] and values["openml"]["count"] == 0  # selected value stays visible
