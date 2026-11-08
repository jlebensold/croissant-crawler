import httpx
import pytest
import respx

from croissant_crawler.http import PoliteClient
from croissant_crawler.sources import get_source
from croissant_crawler.sources.dataverse import DataverseSource

FAST = {"huggingface": 1000, "openml": 1000, "kaggle": 1000, "dataverse": 1000, "web": 1000}


@pytest.fixture
async def client():
    c = PoliteClient(rates=FAST, max_retries=1)
    yield c
    await c.aclose()


@respx.mock
async def test_huggingface_pagination(client):
    nxt = "https://huggingface.co/api/datasets?cursor=abc"
    respx.get("https://huggingface.co/api/datasets", params={"filter": "library:mlcroissant"}).mock(
        return_value=httpx.Response(200, json=[
            {"id": "org/a", "sha": "s1", "downloads": 10, "likes": 1, "tags": ["modality:text"]},
            {"id": "org/private", "private": True},
        ], headers={"link": f'<{nxt}>; rel="next"'}))
    respx.get(nxt).mock(return_value=httpx.Response(200, json=[]))
    src = get_source("huggingface")
    page = await src.discover_page(client, None)
    assert [link.external_id for link in page.links] == ["org/a"]
    assert page.links[0].croissant_url == "https://huggingface.co/api/datasets/org/a/croissant"
    assert page.links[0].source_version == "s1"
    assert page.next_cursor == {"next_url": nxt}
    last = await src.discover_page(client, page.next_cursor)
    assert last.links == [] and last.next_cursor is None


@respx.mock
async def test_openml_end_of_listing(client):
    respx.get(url__regex=r".*/data/list/limit/1000/offset/0/.*").mock(return_value=httpx.Response(200, json={
        "data": {"dataset": [{"did": 31, "name": "credit-g", "version": 1, "file_id": 9,
                              "quality": [{"name": "NumberOfInstances", "value": "1000.0"}]}]}}))
    respx.get(url__regex=r".*/offset/1000/.*").mock(return_value=httpx.Response(412, json={"error": {"code": "372"}}))
    src = get_source("openml")
    page = await src.discover_page(client, None)
    assert page.links[0].croissant_url.endswith("/croissant/dataset/31")
    assert page.links[0].meta["instances"] == 1000.0
    assert page.next_cursor is None  # fewer than a full page
    assert (await src.discover_page(client, {"offset": 1000})).next_cursor is None


@respx.mock
async def test_kaggle_paging(client):
    respx.get("https://www.kaggle.com/api/v1/datasets/list").mock(return_value=httpx.Response(200, json=[
        {"ref": "owner/slug", "currentVersionNumber": 3, "downloadCount": 99, "tags": [{"name": "finance"}]}]))
    page = await get_source("kaggle").discover_page(client, {"page": 7})
    link = page.links[0]
    assert link.croissant_url == "https://www.kaggle.com/datasets/owner/slug/croissant/download"
    assert link.source_version == "3" and link.meta["tags"] == ["finance"]
    assert page.next_cursor == {"page": 8}


@respx.mock
async def test_dataverse_moves_to_next_instance(client):
    src = DataverseSource(instances=["https://dv.one", "https://dv.two"])
    respx.get("https://dv.one/api/search").mock(return_value=httpx.Response(200, json={
        "data": {"total_count": 1, "items": [{"global_id": "doi:10.1/X Y", "versionId": 5}]}}))
    page = await src.discover_page(client, None)
    assert page.next_cursor == {"instance": 1, "start": 0}
    link = page.links[0]
    assert link.external_id == "https://dv.one|doi:10.1/X Y"
    assert "exporter=croissant" in link.croissant_url and "X%20Y" in link.croissant_url
