"""Input-validation guards on the 5 MCP tools."""
from __future__ import annotations

import pytest

from wgea_mcp import server
from wgea_mcp.server import (
    _normalize_dataset_id,
    _validate_filters,
    _validate_period,
)


def test_normalize_dataset_id_uppercases():
    assert _normalize_dataset_id("workforce_composition") == "WORKFORCE_COMPOSITION"


def test_normalize_dataset_id_rejects_non_string():
    with pytest.raises(ValueError, match="must be a string"):
        _normalize_dataset_id(123)
    with pytest.raises(ValueError, match="must be a string"):
        _normalize_dataset_id(None)
    with pytest.raises(ValueError, match="must be a string"):
        _normalize_dataset_id(["a"])


def test_normalize_dataset_id_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        _normalize_dataset_id("")
    with pytest.raises(ValueError, match="empty"):
        _normalize_dataset_id("   ")


def test_normalize_dataset_id_rejects_invalid_chars():
    with pytest.raises(ValueError, match="invalid characters"):
        _normalize_dataset_id("foo-bar")
    with pytest.raises(ValueError, match="invalid characters"):
        _normalize_dataset_id("123BAR")  # must start with letter


def test_validate_filters_none_returns_empty():
    assert _validate_filters(None) == {}


def test_validate_filters_dict_passes():
    assert _validate_filters({"employer_name": "CBA"}) == {"employer_name": "CBA"}


def test_validate_filters_non_dict_raises():
    with pytest.raises(ValueError, match="must be a dict"):
        _validate_filters("CBA")
    with pytest.raises(ValueError, match="must be a dict"):
        _validate_filters(["CBA"])


def test_validate_period_none_returns_none():
    assert _validate_period(None, "start_period") is None


def test_validate_period_empty_returns_none():
    assert _validate_period("", "start_period") is None
    assert _validate_period("  ", "start_period") is None


def test_validate_period_valid_forms():
    assert _validate_period("2024-25", "start_period") == "2024-25"
    assert _validate_period("2024", "start_period") == "2024"


def test_validate_period_invalid_raises():
    with pytest.raises(ValueError, match="invalid format"):
        _validate_period("abc", "start_period")
    with pytest.raises(ValueError, match="invalid format"):
        _validate_period("2024-XX-1", "start_period")


def test_validate_period_non_string_raises():
    with pytest.raises(ValueError, match="must be a string"):
        _validate_period(2024, "start_period")


async def test_search_datasets_rejects_empty_query():
    with pytest.raises(ValueError, match="required"):
        await server.search_datasets(query="", limit=5)


async def test_search_datasets_rejects_non_string():
    with pytest.raises(ValueError, match="must be a string"):
        await server.search_datasets(query=123, limit=5)  # type: ignore[arg-type]


async def test_search_datasets_rejects_limit_below_1():
    # FastMCP's Field validator catches this before our impl, so call .fn directly
    with pytest.raises(ValueError):
        await server.search_datasets(query="x", limit=0)


async def test_list_curated_returns_seven():
    ids = server.list_curated()
    assert len(ids) == 7
    assert "WORKFORCE_COMPOSITION" in ids


async def test_describe_dataset_unknown_raises():
    with pytest.raises(ValueError, match="not a curated"):
        await server.describe_dataset(dataset_id="BOGUS_DATASET")


async def test_get_data_bad_period_raises():
    with pytest.raises(ValueError, match="end_period.*before.*start_period"):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            start_period="2025-26",
            end_period="2023-24",
            format="records",
        )
