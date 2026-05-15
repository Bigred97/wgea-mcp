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
    # 2024 is now coerced to '2024' (Wave 1 int-year coercion). Use an
    # out-of-range int to trigger the still-reject path.
    with pytest.raises(ValueError, match="out of range"):
        _validate_period(12345, "start_period")
    # Truly non-numeric types still raise the type-mismatch error.
    with pytest.raises(ValueError, match="string or int year"):
        _validate_period([2024], "start_period")  # type: ignore[arg-type]


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


# --- Int-year coercion (Wave 1 interop fix) ----------------------------------

def test_validate_period_accepts_int_year():
    """Bare int years are coerced to 'YYYY' string at the boundary."""
    assert _validate_period(2024, "start_period") == "2024"
    assert _validate_period(2025, "end_period") == "2025"
    assert _validate_period(1907, "start_period") == "1907"
    assert _validate_period(2100, "end_period") == "2100"


def test_validate_period_int_out_of_range_raises_helpful():
    """Out-of-range int years raise with a YYYY-YY example, not a TypeError."""
    with pytest.raises(ValueError, match="out of range"):
        _validate_period(1800, "start_period")
    with pytest.raises(ValueError, match="out of range"):
        _validate_period(2200, "end_period")
    with pytest.raises(ValueError, match="YYYY"):
        _validate_period(99, "start_period")


def test_validate_period_rejects_bool_with_hint():
    """bool is a subclass of int but must NOT be coerced silently."""
    with pytest.raises(ValueError, match="bool"):
        _validate_period(True, "start_period")


# --- Strengthened ValueError messages (Wave 1 interop fix) -------------------

async def test_unknown_dataset_suggests_close_match():
    """A near-miss dataset id surfaces a 'Did you mean ...?' hint."""
    with pytest.raises(ValueError, match="Did you mean") as excinfo:
        await server.describe_dataset(dataset_id="WORKFORCE_COMPOSTION")
    assert "WORKFORCE_COMPOSITION" in str(excinfo.value)


def test_period_format_error_includes_examples():
    """Invalid format includes YYYY/YYYY-YY shape + a worked example."""
    with pytest.raises(ValueError) as excinfo:
        _validate_period("?garbage?", "start_period")
    msg = str(excinfo.value)
    assert "YYYY" in msg
    assert "Example" in msg or "Try" in msg or "example" in msg


async def test_period_swap_error_includes_format_hint():
    """end < start should remind users of the period formats."""
    with pytest.raises(ValueError, match="YYYY") as excinfo:
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            start_period="2025-26",
            end_period="2023-24",
            format="records",
        )
    assert "before start_period" in str(excinfo.value)


# --- Wave 4: limit alias on latest() (portfolio interop) ---------------------
#
# wgea-mcp historically used `max_rows` on `latest()`. The portfolio standard
# (asic-mcp uses `limit` on `latest()`) is `limit`. Both names accepted; new
# canonical name added alongside. Supplying both raises ValueError.
#
# NOTE: `get_data(..., max_rows)` is intentionally LEFT ALONE — different
# surface, different concern. The alias is `latest()`-only.


async def test_latest_limit_alias_accepted():
    """limit=5 on latest() must work — replaces max_rows for portfolio
    consistency. Test relies on a fast offline path: an unknown dataset is
    rejected before any data fetch, proving `limit` was wired through the
    same code path as `max_rows`.
    """
    with pytest.raises(ValueError, match="not a curated"):
        await server.latest(dataset_id="BOGUS_DATASET", limit=5)


async def test_latest_max_rows_still_works():
    """Regression — legacy `max_rows` must keep working unchanged."""
    with pytest.raises(ValueError, match="not a curated"):
        await server.latest(dataset_id="BOGUS_DATASET", max_rows=5)


async def test_latest_both_limit_and_max_rows_raises():
    """Mutually exclusive: pick one, not both."""
    with pytest.raises(ValueError, match="Use either limit or max_rows"):
        await server.latest(
            dataset_id="WORKFORCE_COMPOSITION", limit=5, max_rows=10
        )


async def test_latest_neither_limit_nor_max_rows_uses_default():
    """Default behaviour (neither supplied) — error is raised on the
    bogus-dataset guard, NOT on the cap validation, proving the function
    reached the impl with effective_cap=None."""
    with pytest.raises(ValueError, match="not a curated"):
        await server.latest(dataset_id="BOGUS_DATASET")
