"""Regression tests for bugs caught during the v0.1.0 → v0.1.1 QA pass.

Each test pins a behaviour that previously regressed. Keep them.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pandas as pd
import pytest

from wgea_mcp import curated, parsing, server, shaping
from wgea_mcp.cache import Cache
from wgea_mcp.client import WGEAClient


@pytest.fixture
def workforce_df(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_")


# -------------------------------------------------------------------------
# Bug 1: list-of-aliases multi-employer filter returned 0 rows because the
# list path didn't run through the alias map. Fixed by routing each list
# entry through the fuzzy_match_employer path when the column is fuzzy.
# -------------------------------------------------------------------------
def test_bug1_multi_alias_filter_expands(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": ["CBA", "NAB", "Westpac", "ANZ"]},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=2000,
    )
    assert resp.row_count > 0, "Big-4 banks multi-alias filter must return rows"
    employers = {r.dimensions.get("employer_name") for r in resp.records}
    # Should resolve to at least 3 of the Big 4 (fixture might miss one)
    big4_substrings = ["Commonwealth", "National Australia", "Westpac", "Australia And New Zealand"]
    matched_substrings = sum(
        1 for sub in big4_substrings
        if any(sub in (e or "") for e in employers)
    )
    assert matched_substrings >= 3, (
        f"expected ≥3 of Big-4 banks via alias expansion, got "
        f"{matched_substrings} (employers: {employers})"
    )


# -------------------------------------------------------------------------
# Bug 2: negative max_rows silently fell back to default 2000 instead of
# raising. Fixed by adding explicit validation in _get_data_impl.
# -------------------------------------------------------------------------
async def test_bug2_negative_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be >= 1"):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=-5,
        )


async def test_bug2_zero_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be >= 1"):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=0,
        )


async def test_bug2_too_large_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be <="):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=999_999,
        )


async def test_bug2_bool_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be a positive integer"):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=True,  # type: ignore[arg-type]
        )


# -------------------------------------------------------------------------
# Bug 3: filter value None coerced to "None" via str() and matched spuriously
# via rapidfuzz. Fixed by rejecting None filter values up-front.
# -------------------------------------------------------------------------
def test_bug3_none_filter_value_rejected(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match=r"value is None"):
        shaping.build_response(
            cd=cd, df=workforce_df,
            filters={"employer_name": None},
            measures=None, start_period=None, end_period=None,
            fmt="records", user_query={},
        )


def test_bug3_none_in_list_filter_rejected(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match=r"contains None"):
        shaping.build_response(
            cd=cd, df=workforce_df,
            filters={"employer_name": ["CBA", None, "NAB"]},
            measures=None, start_period=None, end_period=None,
            fmt="records", user_query={},
        )


# -------------------------------------------------------------------------
# Bug 4: SQLite read-after-write race caused 50 parallel callers to fire
# 2 HTTP requests for the same URL. Fixed by adding an in-memory LRU of
# recent fetch results that fronts the SQLite cache.
# -------------------------------------------------------------------------
async def test_bug4_in_flight_dedup_50_parallel(tmp_path):
    call_count = 0

    def handler(req):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=b"fake-zip-body")

    transport = httpx.MockTransport(handler)
    cache = Cache(db_path=tmp_path / "c.db")
    client = WGEAClient(cache=cache, transport=transport)
    try:
        url = "https://data.gov.au/data/dataset/X/resource/Y/download/file.zip"
        tasks = [client.fetch_resource(url, kind="data") for _ in range(50)]
        results = await asyncio.gather(*tasks)
        assert all(r == b"fake-zip-body" for r in results)
        assert call_count == 1, (
            f"50 parallel fetches should dedupe to 1 HTTP request "
            f"(got {call_count} — read-after-write race regressed?)"
        )
    finally:
        await client.aclose()


async def test_bug4_recent_results_lru_bounded(tmp_path):
    """The in-memory recent-results LRU must stay bounded."""
    call_count = 0

    def handler(req):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=b"body" + str(call_count).encode())

    transport = httpx.MockTransport(handler)
    cache = Cache(db_path=tmp_path / "c.db")
    client = WGEAClient(cache=cache, transport=transport)
    try:
        # Fetch 20 distinct URLs — recent-results LRU is capped at 16
        for i in range(20):
            url = f"https://data.gov.au/data/r{i}/file.zip"
            await client.fetch_resource(url, kind="data")
        assert len(client._recent_results) <= 16, (
            f"recent-results LRU not bounded — has {len(client._recent_results)}"
        )
    finally:
        await client.aclose()
