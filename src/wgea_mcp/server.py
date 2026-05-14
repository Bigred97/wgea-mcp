"""FastMCP server entrypoint for wgea-mcp.

Five tools, mirroring abs-mcp / rba-mcp / ato-mcp / apra-mcp / aihw-mcp /
asic-mcp so an agent that uses all of them gets a uniform shape:

  - search_datasets    — fuzzy search curated WGEA datasets
  - describe_dataset   — show columns, filters, allowed values for one dataset
  - get_data           — query a dataset with filters / start_period / end_period
  - latest             — shortcut: latest reporting year only
  - list_curated       — enumerate the curated dataset IDs

The MCP shape stays plain-English: users pass `{"employer_name": "Commonwealth Bank"}`
instead of an exact ABN. The fuzzy-match layer translates abbreviations
("CBA", "Westpac", "Woolies") to the source CSV's verbose legal names.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from typing import Annotated, Any, Literal

import pandas as pd
from fastmcp import FastMCP
from pydantic import Field

from . import catalog, curated
from .client import WGEAAPIError, WGEAClient
from .discovery import resolve_latest_zip, resolve_for_year
from .models import (
    ColumnDetail,
    DataResponse,
    DatasetDetail,
    DatasetSummary,
)
from .parsing import drop_blank_rows, read_csv_from_zip
from .shaping import build_response

# Curated IDs are uppercase letters + digits + underscore.
_DATASET_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Period strings: "YYYY-YY" (reporting year label) or "YYYY".
_PERIOD_PATTERN = re.compile(r"^\d{4}(-\d{2,4})?$")
_VALID_FORMATS = {"records", "series", "csv"}
# Cap rows returned in a single query so we don't blow up an agent's context.
# Users can page by tightening filters or paging by reporting_year.
_DEFAULT_MAX_ROWS = 2000
_HARD_MAX_ROWS = 10_000

mcp = FastMCP("wgea-mcp")

_client: WGEAClient | None = None
_client_lock = asyncio.Lock()

# Parsed-DataFrame cache. Even after the byte cache short-circuits the
# network, pandas re-parses the CSV-in-ZIP on every warm call — for the
# 56MB workforce_composition CSV that's ~2s. We cache the post-parse
# DataFrame in-process so repeat queries land in ~50ms. Bounded LRU.
_DF_CACHE_MAX_ENTRIES = 8
_df_cache: OrderedDict[tuple, pd.DataFrame] = OrderedDict()
_df_cache_lock = asyncio.Lock()


def reset_df_cache_for_tests() -> None:
    """Drop the parsed-DataFrame cache."""
    _df_cache.clear()


async def _get_client() -> WGEAClient:
    global _client
    async with _client_lock:
        if _client is None:
            _client = WGEAClient()
        return _client


async def reset_client_for_tests() -> None:
    """Drop the cached client. Tests that span event loops must clear it."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def _normalize_dataset_id(dataset_id: Any) -> str:
    if not isinstance(dataset_id, str):
        raise ValueError(
            f"dataset_id must be a string, got {type(dataset_id).__name__}. "
            "Try search_datasets() or list_curated() to discover IDs."
        )
    norm = dataset_id.strip().upper()
    if not norm:
        raise ValueError(
            "dataset_id is empty. Try list_curated() to see available IDs."
        )
    if not _DATASET_ID_PATTERN.match(norm):
        raise ValueError(
            f"dataset_id {dataset_id!r} contains invalid characters — "
            "wgea-mcp IDs use uppercase letters, digits, and underscores "
            "(e.g. 'WORKFORCE_COMPOSITION', 'HARM_PREVENTION')."
        )
    return norm


def _validate_filters(filters: Any) -> dict[str, Any]:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError(
            f"filters must be a dict, got {type(filters).__name__}. "
            "Example: {'employer_name': 'Commonwealth Bank', 'anzsic_division': 'Mining'}."
        )
    return filters


def _validate_period(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be a string like '2024-25' or '2025', "
            f"got {type(value).__name__}."
        )
    s = value.strip()
    if not s:
        return None
    if not _PERIOD_PATTERN.match(s):
        raise ValueError(
            f"{field_name} {value!r} has invalid format. "
            "Use 'YYYY-YY' (e.g. '2024-25') or 'YYYY' (e.g. '2025')."
        )
    return s


async def _fetch_and_parse(
    cd: curated.CuratedDataset,
) -> tuple[pd.DataFrame, str, str, bool, str | None]:
    """Resolve URL, fetch ZIP bytes, extract CSV, parse to DataFrame.

    Returns (df, zip_url, reporting_year_label, stale, stale_reason).
    """
    client = await _get_client()
    try:
        resolved = await resolve_latest_zip(client)
    except Exception as e:
        raise ValueError(
            f"Could not discover the latest WGEA Public Data File. ({e})"
        ) from e

    try:
        body = await client.fetch_resource(resolved.url, kind="data")
    except WGEAAPIError as e:
        raise ValueError(
            f"Could not fetch WGEA ZIP from data.gov.au. ({e})"
        ) from e

    # Content-aware cache key (mirrors apra-mcp's design).
    head = body[:8192]
    tail = body[-2048:] if len(body) > 8192 else b""
    body_sig = hashlib.sha256(head + tail).digest()
    cache_key = (resolved.url, cd.id, cd.zip_member, len(body), body_sig)

    async with _df_cache_lock:
        cached = _df_cache.get(cache_key)
        if cached is not None:
            _df_cache.move_to_end(cache_key)
            return (
                cached,
                resolved.url,
                resolved.reporting_year_label,
                resolved.stale,
                resolved.reason,
            )

    df = read_csv_from_zip(body, cd.zip_member)

    # Drop trailing blank rows where every key dimension is NaN.
    dim_source_cols = [
        c.source_column for c in cd.columns.values() if c.role == "dimension"
    ]
    if dim_source_cols:
        df = drop_blank_rows(df, dim_source_cols)

    async with _df_cache_lock:
        _df_cache[cache_key] = df
        _df_cache.move_to_end(cache_key)
        while len(_df_cache) > _DF_CACHE_MAX_ENTRIES:
            _df_cache.popitem(last=False)

    return (
        df,
        resolved.url,
        resolved.reporting_year_label,
        resolved.stale,
        resolved.reason,
    )


@mcp.tool
async def search_datasets(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text search query. Matches against dataset IDs, names, "
                "descriptions, and curated search keywords. Case-insensitive."
            ),
            examples=[
                "workforce composition",
                "pay gap analysis",
                "parental leave",
                "sexual harassment",
                "flexible work",
                "managers gender",
                "board diversity",
            ],
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results to return, ranked by relevance.",
            examples=[5, 10],
            ge=1,
            le=50,
        ),
    ] = 10,
) -> list[DatasetSummary]:
    """Fuzzy-search the curated WGEA dataset catalog.

    All seven curated datasets cover the WGEA Public Data File: per-employer
    workforce composition, manager movements, gender-equality policy
    answers, parental-leave + flexible-work policies, harm-prevention
    policies, employee support, and workplace overview.

    Examples:
        # Find datasets about parental leave
        results = await search_datasets("parental leave")
        # → [{id: 'PARENTAL_LEAVE_FLEX', ...}]

        # Find workforce composition by gender
        results = await search_datasets("women in management")

    Returns:
        List of DatasetSummary (id, name, description, update_frequency,
        is_curated), ranked by relevance.
    """
    if not isinstance(query, str):
        raise ValueError(
            f"query must be a string, got {type(query).__name__}. "
            "Try 'workforce', 'pay gap', 'parental leave', or 'harassment'."
        )
    if not query.strip():
        raise ValueError(
            "query is required. Try 'workforce', 'pay gap', 'parental leave', "
            "'harassment', 'flexible work', or any other WGEA topic."
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(
            f"limit must be a positive integer, got {limit!r} ({type(limit).__name__})."
        )
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}.")
    return catalog.search(query, limit=limit)


@mcp.tool
async def describe_dataset(
    dataset_id: Annotated[
        str,
        Field(
            description=(
                "Curated dataset ID. Use search_datasets() to discover or "
                "list_curated() to enumerate. Case-insensitive."
            ),
            examples=[
                "WORKFORCE_COMPOSITION",
                "WORKFORCE_MANAGEMENT",
                "GENDER_EQUALITY_ACTIONS",
                "PARENTAL_LEAVE_FLEX",
                "HARM_PREVENTION",
                "EMPLOYEE_SUPPORT",
                "WORKPLACE_OVERVIEW",
            ],
        ),
    ],
) -> DatasetDetail:
    """Describe a dataset's filterable dimensions, returnable measures, units, and source.

    Use this before calling get_data on a new dataset — it tells you the
    valid filter keys ('employer_name', 'anzsic_division', 'gender', ...),
    enumerated filter values where they exist (e.g. 'women' → 'Women'),
    measure aliases ('n_employees'), and the canonical source URL.

    Returns:
        DatasetDetail with id, name, description, period_coverage, list of
        dimensions, list of measures, source_url, and the resolved
        reporting year label.
    """
    norm_id = _normalize_dataset_id(dataset_id)
    cd = curated.get(norm_id)
    if cd is None:
        raise ValueError(
            f"Dataset {dataset_id!r} is not a curated wgea-mcp dataset. "
            "Try list_curated() to see available IDs."
        )
    dims_out = [
        ColumnDetail(
            key=c.key,
            source_column=c.source_column,
            description=c.description,
            unit=c.unit,
            role=c.role,
        )
        for c in cd.columns.values()
        if c.role in ("dimension", "id")
    ]
    measures_out = [
        ColumnDetail(
            key=c.key,
            source_column=c.source_column,
            description=c.description,
            unit=c.unit,
            role=c.role,
        )
        for c in cd.columns.values()
        if c.role == "measure"
    ]

    # Try a cheap CKAN call to surface the current reporting year — non-fatal.
    year_label: str | None = None
    try:
        client = await _get_client()
        resolved = await resolve_latest_zip(client)
        year_label = resolved.reporting_year_label
    except Exception:
        year_label = None

    return DatasetDetail(
        id=cd.id,
        name=cd.name,
        description=cd.description,
        is_curated=True,
        update_frequency=cd.update_frequency,
        period_coverage=cd.period_coverage,
        dimensions=dims_out,
        measures=measures_out,
        source_url=cd.source_url,
        reporting_year_latest=year_label,
    )


async def _get_data_impl(
    dataset_id: str,
    filters: Any,
    start_period: Any,
    end_period: Any,
    fmt: Any,
    max_rows: int | None = None,
    measures: Any = None,
    latest_only: bool = False,
) -> DataResponse:
    norm_id = _normalize_dataset_id(dataset_id)
    cd = curated.get(norm_id)
    if cd is None:
        raise ValueError(
            f"Dataset {dataset_id!r} is not a curated wgea-mcp dataset. "
            "Try list_curated() to see available IDs."
        )
    filters_d = _validate_filters(filters)
    start_v = _validate_period(start_period, "start_period")
    end_v = _validate_period(end_period, "end_period")
    if fmt is None:
        fmt_norm = "records"
    elif isinstance(fmt, str):
        fmt_norm = fmt.lower()
    else:
        raise ValueError(
            f"format must be a string, got {type(fmt).__name__}. "
            f"Valid options: {sorted(_VALID_FORMATS)}"
        )
    if fmt_norm not in _VALID_FORMATS:
        raise ValueError(
            f"Unknown format {fmt!r}. Valid options: {sorted(_VALID_FORMATS)}"
        )
    if start_v and end_v and start_v > end_v:
        raise ValueError(
            f"end_period ({end_v}) is before start_period ({start_v}). "
            "Try swapping them."
        )

    user_query: dict[str, Any] = {}
    if filters_d:
        user_query["filters"] = dict(filters_d)
    if measures is not None:
        user_query["measures"] = measures
    if start_v:
        user_query["start_period"] = start_v
    if end_v:
        user_query["end_period"] = end_v

    df, url_used, year_label, stale, stale_reason = await _fetch_and_parse(cd)

    # If latest_only, restrict to just the resolved reporting year.
    if latest_only:
        if cd.period_column in df.columns:
            df = df.loc[df[cd.period_column].astype("string") == year_label]
            df = df.reset_index(drop=True)

    if max_rows is not None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int):
            raise ValueError(
                f"max_rows must be a positive integer (1-{_HARD_MAX_ROWS}), "
                f"got {max_rows!r} ({type(max_rows).__name__})."
            )
        if max_rows < 1:
            raise ValueError(
                f"max_rows must be >= 1, got {max_rows}. Omit the parameter "
                f"to use the default cap of {_DEFAULT_MAX_ROWS}."
            )
        if max_rows > _HARD_MAX_ROWS:
            raise ValueError(
                f"max_rows must be <= {_HARD_MAX_ROWS}, got {max_rows}. "
                "For larger queries, paginate by reporting_year or tighten filters."
            )
        effective_max = max_rows
    else:
        effective_max = _DEFAULT_MAX_ROWS
    return build_response(
        cd=cd,
        df=df,
        filters=filters_d,
        measures=measures,
        start_period=start_v,
        end_period=end_v,
        fmt=fmt_norm,
        user_query=user_query,
        download_url=url_used,
        source_url=cd.source_url,
        stale=stale,
        stale_reason=stale_reason,
        max_rows=effective_max,
    )


@mcp.tool
async def get_data(
    dataset_id: Annotated[
        str,
        Field(
            description="Curated dataset ID. Use search_datasets() / list_curated().",
            examples=[
                "WORKFORCE_COMPOSITION",
                "WORKFORCE_MANAGEMENT",
                "GENDER_EQUALITY_ACTIONS",
                "PARENTAL_LEAVE_FLEX",
                "HARM_PREVENTION",
            ],
        ),
    ],
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Dimension filters. Keys are plain-English aliases from the "
                "dataset's describe_dataset response. Values are matched "
                "against the source data; pass a list to OR across values. "
                "Permissive dimensions (e.g. employer_name, question_text) "
                "accept any string and support fuzzy matching — try "
                "{'employer_name': 'CBA'} or {'employer_name': 'commonwealth*'} "
                "for wildcard substring search."
            ),
            examples=[
                {"employer_name": "Commonwealth Bank"},
                {"anzsic_division": "Mining"},
                {"employer_name": "CBA", "gender": "women"},
                {"employer_name": ["CBA", "NAB", "Westpac", "ANZ"]},
                {"section": "Gender Pay Gap", "response": "Yes"},
            ],
        ),
    ] = None,
    start_period: Annotated[
        str | None,
        Field(
            description=(
                "Inclusive start reporting year. Format: 'YYYY-YY' (e.g. "
                "'2023-24') or 'YYYY' (matched against WGEA's reporting_year "
                "column)."
            ),
            examples=["2023-24", "2024-25", "2023"],
        ),
    ] = None,
    end_period: Annotated[
        str | None,
        Field(
            description="Inclusive end reporting year. Same format as start_period.",
            examples=["2024-25", "2025-26"],
        ),
    ] = None,
    format: Annotated[
        Literal["records", "series", "csv"],
        Field(
            description=(
                "Response shape. 'records' (default): flat list of observations. "
                "'series': grouped by measure. 'csv': pandas CSV string in "
                "`csv` field."
            ),
            examples=["records", "series", "csv"],
        ),
    ] = "records",
    max_rows: Annotated[
        int | None,
        Field(
            description=(
                "Cap on returned rows after filtering. Default 2000. Max "
                "10000. Tighten filters to narrow further."
            ),
            examples=[100, 500, 2000],
            ge=1,
            le=10_000,
        ),
    ] = None,
) -> DataResponse:
    """Query a curated WGEA dataset and return observations.

    Examples:
        # Gender breakdown at Commonwealth Bank
        resp = await get_data(
            "WORKFORCE_COMPOSITION",
            filters={"employer_name": "Commonwealth Bank"},
        )

        # Promotions to manager by gender at Westpac in 2024-25
        resp = await get_data(
            "WORKFORCE_MANAGEMENT",
            filters={"employer_name": "Westpac", "movement_type": "Promotions",
                     "manager_category": "Managers"},
        )

        # Which employers in mining set gender targets?
        resp = await get_data(
            "GENDER_EQUALITY_ACTIONS",
            filters={"anzsic_division": "Mining",
                     "section": "Gender Pay Gap",
                     "response": "Yes"},
        )

        # Sexual harassment policy responses across financial services
        resp = await get_data(
            "HARM_PREVENTION",
            filters={"anzsic_division": "Financial and Insurance Services",
                     "subsection": "Sexual Harassment"},
        )

    Returns:
        DataResponse with records (or csv), unit, reporting_year, row_count,
        source URL, the actual download_url used, "did you mean?" fuzzy hints
        if the employer-name filter didn't match exactly, and CC-BY 3.0 AU
        attribution.
    """
    return await _get_data_impl(
        dataset_id, filters, start_period, end_period, format, max_rows
    )


@mcp.tool
async def latest(
    dataset_id: Annotated[
        str,
        Field(
            description="Curated dataset ID.",
            examples=[
                "WORKFORCE_COMPOSITION",
                "WORKFORCE_MANAGEMENT",
                "PARENTAL_LEAVE_FLEX",
            ],
        ),
    ],
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description="Same filter shape as get_data. Useful for narrowing to one employer.",
            examples=[
                {"employer_name": "Commonwealth Bank"},
                {"employer_name": "QANTAS", "manager_category": "Manager"},
                {"anzsic_division": "Retail Trade"},
            ],
        ),
    ] = None,
    max_rows: Annotated[
        int | None,
        Field(
            description="Cap on returned rows. Default 2000, max 10000.",
            ge=1,
            le=10_000,
        ),
    ] = None,
) -> DataResponse:
    """Return rows from the most recent WGEA reporting year for a dataset.

    Trims to the single latest reporting_year — useful for "what's the
    current gender breakdown at CBA?" without having to remember WGEA's
    annual cadence.

    Examples:
        # Latest workforce composition at CBA
        resp = await latest("WORKFORCE_COMPOSITION",
                            filters={"employer_name": "Commonwealth Bank"})
    """
    return await _get_data_impl(
        dataset_id, filters, None, None, "records", max_rows, latest_only=True
    )


@mcp.tool
def list_curated() -> list[str]:
    """List every curated dataset ID in this version of wgea-mcp.

    Returns:
        Sorted list of dataset IDs.
    """
    return curated.list_ids()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
