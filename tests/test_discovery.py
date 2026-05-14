"""CKAN discovery — package_show parsing, year resolution, seed fallback."""
from __future__ import annotations

import json

import httpx
import pytest
from respx import MockRouter

from wgea_mcp import discovery
from wgea_mcp.cache import Cache
from wgea_mcp.client import WGEAClient
from wgea_mcp.discovery import (
    DiscoveryError,
    list_public_data_files,
    load_seed_manifest,
    resolve_for_year,
    resolve_latest_zip,
    seed_manifest_metadata,
)


@pytest.fixture
async def client(tmp_path):
    cache = Cache(db_path=tmp_path / "cache.db")
    c = WGEAClient(cache=cache)
    try:
        yield c
    finally:
        await c.aclose()


async def test_list_public_data_files_picks_zip_resources(
    client, respx_mock, fake_ckan_response_bytes
):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=fake_ckan_response_bytes)
    items = await list_public_data_files(client)
    # Three "Public Data File" resources match; the "List of Organisations" and
    # "Specifications" should be filtered out.
    assert len(items) == 3
    assert items[0].reporting_year_start == 2025
    assert items[0].reporting_year_label == "2024-25"
    assert items[-1].reporting_year_start == 2023


async def test_list_public_data_files_sorted_newest_first(
    client, respx_mock, fake_ckan_response_bytes
):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=fake_ckan_response_bytes)
    items = await list_public_data_files(client)
    years = [i.reporting_year_start for i in items]
    assert years == sorted(years, reverse=True)


async def test_resolve_latest_zip_returns_newest(
    client, respx_mock, fake_ckan_response_bytes
):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=fake_ckan_response_bytes)
    resolved = await resolve_latest_zip(client)
    assert resolved.reporting_year_start == 2025
    assert resolved.reporting_year_label == "2024-25"
    assert resolved.tier == "ckan"
    assert resolved.stale is False
    assert resolved.url.endswith("wgea_public_dataset_2025.zip")


async def test_resolve_for_year_specific(client, respx_mock, fake_ckan_response_bytes):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=fake_ckan_response_bytes)
    resolved = await resolve_for_year(client, 2024)
    assert resolved.reporting_year_start == 2024
    assert resolved.url.endswith("wgea_public_dataset_2024.zip")


async def test_resolve_for_year_missing_raises(
    client, respx_mock, fake_ckan_response_bytes
):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=fake_ckan_response_bytes)
    with pytest.raises(DiscoveryError):
        await resolve_for_year(client, 2099)


async def test_resolve_latest_zip_seed_fallback(client, respx_mock):
    """When CKAN is unreachable, the seed manifest URL is returned with stale=True."""
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(500)
    resolved = await resolve_latest_zip(client)
    assert resolved.tier == "seed"
    assert resolved.stale is True
    assert resolved.url.endswith(".zip")
    assert resolved.reason is not None


async def test_list_public_data_files_bad_payload(client, respx_mock):
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=b'{"success": true, "result": {"resources": "not a list"}}')
    with pytest.raises(DiscoveryError, match="malformed"):
        await list_public_data_files(client)


def test_load_seed_manifest_returns_dict():
    seed = load_seed_manifest()
    assert isinstance(seed, dict)
    # 2025 entry should exist
    assert 2025 in seed
    assert seed[2025].startswith("https://")


def test_seed_manifest_metadata():
    meta = seed_manifest_metadata()
    assert "refreshed_at" in meta or "generated_at" in meta or meta == {}


def test_year_to_label_helper():
    assert discovery._year_to_label(2025) == "2024-25"
    assert discovery._year_to_label(2024) == "2023-24"
    assert discovery._year_to_label(2020) == "2019-20"


async def test_resolve_latest_zip_no_match_no_seed_raises(client, respx_mock, monkeypatch):
    """If CKAN gives no matches and seed is empty, raise."""
    respx_mock.get(
        "https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset"
    ).respond(200, content=b'{"success": true, "result": {"resources": []}}')
    monkeypatch.setattr(discovery, "load_seed_manifest", lambda: {})
    with pytest.raises(DiscoveryError):
        await resolve_latest_zip(client)
