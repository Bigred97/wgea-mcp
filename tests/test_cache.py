"""SQLite cache — get/set, TTL, corruption recovery."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from wgea_mcp.cache import TTL, Cache


@pytest.fixture
async def cache(tmp_path):
    c = Cache(db_path=tmp_path / "cache.db")
    yield c


async def test_get_missing_returns_none(cache):
    assert await cache.get("nonexistent", timedelta(seconds=60)) is None


async def test_set_and_get(cache):
    await cache.set("k1", b"value1", kind="data")
    assert await cache.get("k1", timedelta(seconds=60)) == b"value1"


async def test_ttl_expiry(cache):
    await cache.set("k1", b"value1", kind="data")
    # Zero TTL should always miss
    assert await cache.get("k1", timedelta(seconds=0)) is None


async def test_set_with_etag(cache):
    await cache.set("k1", b"v", kind="landing", etag="\"abc\"", last_modified="Mon")
    cached_at = await cache.get_cached_at("k1")
    assert cached_at is not None


async def test_clear_by_kind(cache):
    await cache.set("a", b"x", kind="data")
    await cache.set("b", b"y", kind="catalog")
    await cache.clear(kind="data")
    assert await cache.get("a", timedelta(seconds=60)) is None
    assert await cache.get("b", timedelta(seconds=60)) == b"y"


async def test_clear_all(cache):
    await cache.set("a", b"x", kind="data")
    await cache.set("b", b"y", kind="catalog")
    await cache.clear()
    assert await cache.get("a", timedelta(seconds=60)) is None
    assert await cache.get("b", timedelta(seconds=60)) is None


async def test_corrupt_db_self_heals(tmp_path):
    p = tmp_path / "cache.db"
    p.write_bytes(b"not a sqlite database")
    c = Cache(db_path=p)
    # First operation should detect corruption and rebuild
    await c.set("k", b"v", kind="data")
    assert await c.get("k", timedelta(seconds=60)) == b"v"


async def test_ttl_table_is_complete():
    assert "data" in TTL
    assert "catalog" in TTL
    assert "landing" in TTL
    assert "discovery" in TTL
    # Data should be 30 days (annual cadence)
    assert TTL["data"] == timedelta(days=30)
