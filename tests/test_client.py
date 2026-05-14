"""WGEAClient — host whitelist, CKAN package_show, in-flight dedup, stale fallback."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite
import httpx
import pytest

from wgea_mcp.cache import Cache
from wgea_mcp.client import (
    WGEAAPIError,
    WGEAClient,
    _is_allowed_host,
    get_stale_signal,
    reset_stale_signal,
)


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


# ─── stale-fallback graceful degradation (CLAUDE.md quality dim #4) ──────

async def _prime_stale_cache(
    db_path: Path, url: str, payload: bytes, age_hours: float
) -> None:
    """Seed the cache with `payload` as if fetched `age_hours` ago.

    Bypasses the public `Cache.set` so we can rewrite `cached_at` to a
    past timestamp — a normal `cache.get` with the per-kind TTL will MISS
    this row (cached_at older than the TTL window), but `cache.get_stale`
    still returns it. Mirrors abs-mcp's `_prime_stale_cache` helper.
    """
    cache = Cache(db_path=db_path)
    await cache._ensure_init()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO http_cache (cache_key, payload, cached_at, kind) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload, cached_at=excluded.cached_at",
            (url, payload, time.time() - age_hours * 3600, "data"),
        )
        await conn.commit()


async def test_stale_fallback_serves_cached_payload_on_5xx(tmp_path):
    """When data.gov.au returns 5xx and we have a cached payload past its TTL,
    serve the cached payload and mark the response as stale. Agents continue
    reasoning rather than crashing."""
    db_path = tmp_path / "cache.db"
    url = "https://data.gov.au/data/dataset/wgea/resource/x/download/wgea_2025.zip"
    payload = b"cached-wgea-zip-bytes"
    # Prime a 48-day-old entry — well past the 30-day "data" TTL, so cache.get
    # misses but cache.get_stale still returns.
    await _prime_stale_cache(db_path, url, payload, age_hours=48 * 24)

    reset_stale_signal()
    cache = Cache(db_path=db_path)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(503, text="Service Unavailable")
    )
    async with WGEAClient(cache=cache, transport=transport) as client:
        result = await client.fetch_resource(url, kind="data")
        assert result == payload, "fallback must return the cached payload"
        stale, reason = get_stale_signal()
        assert stale is True, "stale flag must be set after 5xx fallback"
        assert reason and "503" in reason, (
            f"stale_reason should mention the 5xx status: {reason!r}"
        )
        assert "minute" in reason.lower(), (
            f"stale_reason should report cache age: {reason!r}"
        )


async def test_stale_fallback_serves_cached_on_request_error(tmp_path):
    """Same as 5xx test but for httpx.RequestError (DNS / connection refused)."""
    db_path = tmp_path / "cache.db"
    url = "https://data.gov.au/data/dataset/wgea/resource/x/download/wgea_2025.zip"
    payload = b"cached-wgea-zip-bytes"
    await _prime_stale_cache(db_path, url, payload, age_hours=48 * 24)

    def raise_connect_error(req):
        raise httpx.ConnectError("simulated DNS failure")

    reset_stale_signal()
    cache = Cache(db_path=db_path)
    transport = httpx.MockTransport(raise_connect_error)
    async with WGEAClient(cache=cache, transport=transport) as client:
        result = await client.fetch_resource(url, kind="data")
        assert result == payload
        stale, reason = get_stale_signal()
        assert stale is True
        assert reason and "ConnectError" in reason, (
            f"stale_reason should mention ConnectError: {reason!r}"
        )


async def test_raises_when_no_stale_cache_to_fall_back_to(tmp_path):
    """Empty cache + upstream 5xx → still raises WGEAAPIError (original behaviour
    when there's nothing to gracefully degrade to)."""
    db_path = tmp_path / "cache.db"
    url = "https://data.gov.au/data/dataset/wgea/resource/x/download/wgea_2025.zip"
    # No cache priming — fall-back must fail closed.

    reset_stale_signal()
    cache = Cache(db_path=db_path)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(503, text="Service Unavailable")
    )
    async with WGEAClient(cache=cache, transport=transport) as client:
        with pytest.raises(WGEAAPIError, match="503"):
            await client.fetch_resource(url, kind="data")


async def test_cache_get_stale_returns_payload_and_timestamp(tmp_path):
    """Cache.get_stale() returns (payload, cached_at) regardless of TTL —
    the building block for the client's stale-fallback path."""
    from datetime import timedelta

    db_path = tmp_path / "cache.db"
    cache = Cache(db_path=db_path)
    await cache.set("https://example.org/x", b"hello", kind="data")
    # Normal `get` with a zero TTL should miss
    fresh = await cache.get("https://example.org/x", ttl=timedelta(seconds=0))
    assert fresh is None
    # `get_stale` should return regardless of TTL
    stale = await cache.get_stale("https://example.org/x")
    assert stale is not None
    payload, cached_at = stale
    assert payload == b"hello"
    assert cached_at > 0
    # Non-existent key → None
    miss = await cache.get_stale("https://example.org/missing")
    assert miss is None
