"""WGEAClient — host whitelist, CKAN package_show, in-flight dedup."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from wgea_mcp.cache import Cache
from wgea_mcp.client import WGEAAPIError, WGEAClient, _is_allowed_host


@pytest.fixture
async def client(tmp_path):
    cache = Cache(db_path=tmp_path / "cache.db")
    c = WGEAClient(cache=cache)
    try:
        yield c
    finally:
        await c.aclose()


def test_host_whitelist_accepts_data_gov_au():
    assert _is_allowed_host("https://data.gov.au/file.zip")
    assert _is_allowed_host("https://www.data.gov.au/file.zip")
    assert _is_allowed_host("https://api.data.gov.au/x")


def test_host_whitelist_rejects_others():
    assert not _is_allowed_host("https://example.com/file.zip")
    assert not _is_allowed_host("https://evil.org/x")
    assert not _is_allowed_host("ftp://data.gov.au/file.zip")
    assert not _is_allowed_host("not-a-url")
    assert not _is_allowed_host("")


async def test_fetch_resource_rejects_non_data_gov_au(client):
    with pytest.raises(WGEAAPIError, match="off-host"):
        await client.fetch_resource("https://example.com/anything.zip")


async def test_fetch_resource_rejects_non_http(client):
    with pytest.raises(WGEAAPIError, match="non-http"):
        await client.fetch_resource("file:///etc/passwd")


async def test_fetch_package_rejects_bad_id(client):
    with pytest.raises(WGEAAPIError, match="Bad package id"):
        await client.fetch_package("a/../b")
    with pytest.raises(WGEAAPIError, match="Bad package id"):
        await client.fetch_package("a?b")
    with pytest.raises(WGEAAPIError, match="Bad package id"):
        await client.fetch_package("a&b")


async def test_fetch_package_success(client, respx_mock, fake_ckan_response_bytes):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=fake_ckan_response_bytes)
    result = await client.fetch_package("wgea-dataset")
    assert result["name"] == "wgea-dataset"
    assert isinstance(result["resources"], list)


async def test_fetch_package_failure(client, respx_mock):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=does-not-exist"
    ).respond(
        200,
        content=b'{"success": false, "error": {"message": "Not found"}}',
    )
    with pytest.raises(WGEAAPIError, match="CKAN error"):
        await client.fetch_package("does-not-exist")


async def test_fetch_resource_caches(client, respx_mock):
    body = b"fake-zip-content"
    route = respx_mock.get("https://data.gov.au/data/test.zip").respond(200, content=body)
    out1 = await client.fetch_resource("https://data.gov.au/data/test.zip")
    out2 = await client.fetch_resource("https://data.gov.au/data/test.zip")
    assert out1 == body and out2 == body
    assert route.call_count == 1  # cached on second call


async def test_fetch_package_non_json(client, respx_mock):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=b"not json at all")
    with pytest.raises(WGEAAPIError, match="non-JSON"):
        await client.fetch_package("wgea-dataset")
