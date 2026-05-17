"""Shared pytest fixtures.

A small fixture ZIP (`tests/fixtures/wgea_sample.zip`) is produced from the
real 2025 WGEA Public Data File by truncating each thematic CSV to ~200 rows
that include rows for a handful of well-known employers (Commonwealth Bank,
NAB, Westpac, Qantas, Atlassian, Telstra). The fixture stays under 50 KB so
the repo doesn't bloat.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wgea_mcp import curated, shaping

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_ZIP_PATH = FIXTURE_DIR / "wgea_sample.zip"
# Truncated EGPG xlsx fixture for HEADLINE_GAP — 9 private-sector employer
# rows across Mining/Finance/Construction + 1 Commonwealth row that must
# be filtered out by the aggregator. Kept under 10 KB so the wheel stays
# lean (real WGEA xlsx is ~2 MB).
SAMPLE_EGPG_XLSX_PATH = FIXTURE_DIR / "wgea_egpg_sample.xlsx"


@pytest.fixture(autouse=True)
def reset_curated_registry():
    curated.reset_registry()
    shaping.reset_alias_cache_for_tests()
    yield
    curated.reset_registry()
    shaping.reset_alias_cache_for_tests()


@pytest.fixture(autouse=True)
def isolate_parquet_cache_dir(tmp_path_factory, monkeypatch):
    """Redirect the Parquet on-disk cache to a per-session tmp dir.

    Without this, tests would write to `~/.wgea-mcp/parquet-cache/`
    (the real user dir) and cache hits would leak between test runs
    and across developer machines.
    """
    target = tmp_path_factory.mktemp("wgea_parquet_cache")
    monkeypatch.setenv("WGEA_MCP_PARQUET_CACHE_DIR", str(target))
    yield


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def sample_zip_bytes() -> bytes:
    """The truncated WGEA ZIP shipped under tests/fixtures."""
    return SAMPLE_ZIP_PATH.read_bytes()


@pytest.fixture
def sample_egpg_xlsx_bytes() -> bytes:
    """The truncated EGPG xlsx fixture (HEADLINE_GAP source)."""
    return SAMPLE_EGPG_XLSX_PATH.read_bytes()


@pytest.fixture
def fake_ckan_response() -> dict[str, Any]:
    """A representative CKAN package_show payload for the wgea-dataset slug."""
    return {
        "success": True,
        "result": {
            "name": "wgea-dataset",
            "title": "WGEA Dataset",
            "license_id": "cc-by",
            "license_title": "Creative Commons Attribution 3.0 Australia",
            "license_url": "http://creativecommons.org/licenses/by/3.0/au/",
            "metadata_modified": "2026-01-09T11:03:34.645736",
            "resources": [
                {
                    "name": "2025 WGEA Data - Public Data File",
                    "format": "CSV",
                    "url": "https://data.gov.au/data/dataset/4d35cd80-2538-4705-82f3-d0d18e823d98/resource/380faa66-1126-4020-8b89-496821290624/download/wgea_public_dataset_2025.zip",
                    "size": 74406050,
                    "last_modified": "2026-01-08T23:55:10.731609",
                },
                {
                    "name": "2024 WGEA Data - Public Data File",
                    "format": "ZIP",
                    "url": "https://data.gov.au/data/dataset/4d35cd80-2538-4705-82f3-d0d18e823d98/resource/f12cc138-44a8-45fc-9ba7-97ee5dadd683/download/wgea_public_dataset_2024.zip",
                    "size": 67501600,
                    "last_modified": "2025-07-30T02:20:49.683449",
                },
                {
                    "name": "2023 WGEA Data - Public Data File",
                    "format": ".csv",
                    "url": "https://data.gov.au/data/dataset/4d35cd80-2538-4705-82f3-d0d18e823d98/resource/4f716314-5de2-425b-aef2-6501c0be076f/download/wgea_public_dataset_2023.zip",
                    "size": 24107678,
                    "last_modified": "2024-01-07T00:00:00",
                },
                # Other resources that should be ignored by the discovery filter.
                {
                    "name": "2022 WGEA Data - List of Organisations by ABN",
                    "format": ".csv",
                    "url": "https://data.gov.au/dummy.csv",
                    "size": 835240,
                    "last_modified": "2022-12-11T00:00:00",
                },
                {
                    "name": "2022 WGEA Data - Specifications",
                    "format": "excel (.xlsx)",
                    "url": "https://data.gov.au/dummy.xlsx",
                    "size": 11611,
                    "last_modified": "2022-12-11T00:00:00",
                },
            ],
        },
    }


@pytest.fixture
def fake_ckan_response_bytes(fake_ckan_response: dict[str, Any]) -> bytes:
    return json.dumps(fake_ckan_response).encode("utf-8")
