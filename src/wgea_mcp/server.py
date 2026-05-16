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
from .client import (
    WGEAAPIError,
    WGEAClient,
    get_stale_signal,
    reset_stale_signal,
)
from .discovery import resolve_latest_zip
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


def _fuzzy_suggest(query: str, candidates: list[str], cutoff: int = 60) -> str | None:
    """Return the closest candidate string if it scores >= cutoff on RapidFuzz WRatio.

    Falls back to None if rapidfuzz is unavailable or no match clears the bar.
    The 60 cutoff catches realistic typos ('WORKFORCE_COMPOSTION' ->
    'WORKFORCE_COMPOSITION') without firing on unrelated near-misses.
    """
    if not query or not candidates:
        return None
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return None
    match = process.extractOne(query, candidates, scorer=fuzz.WRatio, score_cutoff=cutoff)
    return match[0] if match else None


def _unknown_dataset_msg(dataset_id: str) -> str:
    """Build a 'not curated' error message with a 'Did you mean ...' hint and
    a truncated list of valid IDs."""
    ids = curated.list_ids()
    norm = dataset_id.strip().upper() if isinstance(dataset_id, str) else ""
    suggestion = _fuzzy_suggest(norm, ids, cutoff=60)
    suggest_msg = f"Did you mean {suggestion!r}? " if suggestion else ""
    shown = ids[:10]
    rest = f" ({len(ids)} total)" if len(ids) > len(shown) else ""
    return (
        f"Dataset {dataset_id!r} is not a curated wgea-mcp dataset. "
        f"{suggest_msg}Valid options: {', '.join(shown)}{rest}. "
        "Try list_curated() to enumerate, or search_datasets('<topic>') to find by keyword."
    )


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
    if isinstance(filters, str):
        import json as _json
        try:
            filters = _json.loads(filters)
        except _json.JSONDecodeError as exc:
            raise ValueError(
                f"filters must be a JSON object, got invalid JSON string: {exc}. "
                "Example: {\"employer_name\": \"Commonwealth Bank\", \"anzsic_division\": \"Mining\"}."
            ) from exc
    if not isinstance(filters, dict):
        raise ValueError(
            f"filters must be a dict, got {type(filters).__name__}. "
            "Example: {'employer_name': 'Commonwealth Bank', 'anzsic_division': 'Mining'}."
        )
    return filters


def _validate_period(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    # LLM clients routinely send JSON ints (e.g. {"start_period": 2024}). Coerce
    # 4-digit ints in a realistic WGEA reporting-year range to the canonical
    # "YYYY" string at the boundary so we don't surface a confusing type error.
    if isinstance(value, bool):
        # bool is a subclass of int; reject it explicitly before the int branch.
        raise ValueError(
            f"{field_name} must be a string or int year, got bool. "
            f"Try {field_name}='2024-25' (WGEA reporting year), '2024' (year), "
            "or 2024 (int year)."
        )
    if isinstance(value, int):
        if 1900 <= value <= 2100:
            value = str(value)
        else:
            raise ValueError(
                f"{field_name} integer {value} out of range. "
                f"For year-only periods pass a 4-digit year like 2024, or use string "
                f"forms 'YYYY' (e.g. '2024') or 'YYYY-YY' (e.g. '2024-25'). "
                f"Try {field_name}='2024-25'."
            )
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be a string or int year, got {type(value).__name__}. "
            f"Try {field_name}='2024-25' (WGEA reporting year), '2024' (year), "
            "or 2024 (int year)."
        )
    s = value.strip()
    if not s:
        return None
    if not _PERIOD_PATTERN.match(s):
        guess = s[:4] if s[:4].isdigit() else "2024"
        raise ValueError(
            f"{field_name} {value!r} has invalid format. "
            "Period formats: 'YYYY-YY' (e.g. '2024-25' — WGEA reporting year) or "
            "'YYYY' (e.g. '2024'). "
            f"Did you mean {guess!r}? "
            f"Example: get_data('WORKFORCE_COMPOSITION', start_period='2023-24', "
            "end_period='2024-25')."
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
        raise ValueError(_unknown_dataset_msg(dataset_id))
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
    # Reset the per-context stale signal at the top of every tool invocation
    # so a previous fetch's flag can't leak into this response. Mirrors the
    # abs-mcp graceful-degradation pattern: when data.gov.au is unreachable
    # and WGEAClient falls back to a cached ZIP past its TTL, the signal
    # propagates up here and we surface it on the response.
    reset_stale_signal()
    norm_id = _normalize_dataset_id(dataset_id)
    cd = curated.get(norm_id)
    if cd is None:
        raise ValueError(_unknown_dataset_msg(dataset_id))
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
            f"Valid options: {sorted(_VALID_FORMATS)}. "
            "Try format='records' (default), 'series', or 'csv'."
        )
    if fmt_norm not in _VALID_FORMATS:
        valid_sorted = sorted(_VALID_FORMATS)
        suggestion = _fuzzy_suggest(fmt_norm, valid_sorted, cutoff=60)
        suggest_msg = f"Did you mean {suggestion!r}? " if suggestion else ""
        raise ValueError(
            f"Unknown format {fmt!r}. {suggest_msg}"
            f"Valid options: {valid_sorted}. "
            "Try format='records' (default), 'series', or 'csv'."
        )
    if start_v and end_v and start_v > end_v:
        raise ValueError(
            f"end_period ({end_v}) is before start_period ({start_v}). "
            f"Try swapping them: start_period={end_v!r}, end_period={start_v!r}. "
            "Period formats: 'YYYY-YY' (e.g. '2024-25' — WGEA reporting year) or "
            "'YYYY' (e.g. '2024')."
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
    response = build_response(
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
    # Merge in the HTTP-level stale signal (set by WGEAClient when it served
    # a cached payload past its TTL because data.gov.au was unreachable).
    # The seed-manifest path already set response.stale via build_response;
    # if that's already True we keep its (more informative) reason. Otherwise
    # we adopt the HTTP-fallback reason.
    http_stale, http_reason = get_stale_signal()
    if http_stale and not response.stale:
        response.stale = True
        response.stale_reason = http_reason
    return response


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
        str | int | None,
        Field(
            description=(
                "Inclusive start reporting year. Format: 'YYYY-YY' (e.g. "
                "'2023-24') or 'YYYY' (matched against WGEA's reporting_year "
                "column). Bare int years like 2023 are coerced to '2023' "
                "automatically."
            ),
            examples=["2023-24", "2024-25", "2023", 2023],
        ),
    ] = None,
    end_period: Annotated[
        str | int | None,
        Field(
            description="Inclusive end reporting year. Same format as start_period.",
            examples=["2024-25", "2025-26", 2024],
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
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Cap on returned rows (portfolio-standard name). Default 2000, "
                "max 10000. Mutually exclusive with the legacy `max_rows` "
                "alias — supplying both raises ValueError."
            ),
            ge=1,
            le=10_000,
            examples=[100, 500, 2000],
        ),
    ] = None,
    max_rows: Annotated[
        int | None,
        Field(
            description=(
                "Legacy alias for `limit` — retained for backward compatibility "
                "(wgea-mcp <= 0.4.x). Prefer `limit` for cross-sister consistency "
                "with asic-mcp's `latest(..., limit)` parameter. Same semantics "
                "as `limit`. Supplying both raises ValueError."
            ),
            ge=1,
            le=10_000,
            examples=[100, 500, 2000],
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

        # Cap rows (portfolio-standard name)
        resp = await latest("WORKFORCE_COMPOSITION",
                            filters={"anzsic_division": "Mining"}, limit=100)

        # Legacy alias still works
        resp = await latest("WORKFORCE_COMPOSITION",
                            filters={"anzsic_division": "Mining"}, max_rows=100)

    Parameter notes:
        - Prefer `limit` (portfolio-standard; matches asic-mcp's
          `latest(..., limit)` parameter).
        - `max_rows` retained as legacy alias.
        - Supplying both raises ValueError — pick one.
        - `get_data()` keeps `max_rows` unchanged (separate surface, separate
          concern).
    """
    if limit is not None and max_rows is not None:
        raise ValueError(
            f"Use either limit or max_rows, not both. "
            f"Got limit={limit!r} and max_rows={max_rows!r}. "
            "limit is the portfolio-standard name; max_rows is retained as a "
            "legacy alias."
        )
    effective_cap = limit if limit is not None else max_rows
    return await _get_data_impl(
        dataset_id, filters, None, None, "records", effective_cap, latest_only=True
    )


@mcp.tool
async def top_n(
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
    measure: Annotated[
        str,
        Field(
            description=(
                "Numeric measure column to rank by. WGEA measures are "
                "`n_employees` (WORKFORCE_COMPOSITION, WORKFORCE_MANAGEMENT) "
                "or `n_responses` (the other five questionnaire datasets). "
                "Use describe_dataset() to confirm."
            ),
            examples=["n_employees", "n_responses"],
        ),
    ],
    n: Annotated[
        int,
        Field(
            description="How many top (or bottom) rows to return.",
            ge=1,
            le=100,
            examples=[5, 10, 20, 50],
        ),
    ] = 10,
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description="Optional dimension filters, same shape as get_data.",
            examples=[
                {"gender": "Women", "manager_category": "Manager"},
                {"anzsic_division": "Mining"},
                {"section": "Gender Pay Gap", "response": "Yes"},
            ],
        ),
    ] = None,
    direction: Annotated[
        Literal["top", "bottom"],
        Field(
            description=(
                "'top' returns the N rows with the LARGEST measure values "
                "(highest n_employees, biggest n_responses, etc.). "
                "'bottom' returns the SMALLEST."
            ),
            examples=["top", "bottom"],
        ),
    ] = "top",
    reporting_year: Annotated[
        str | int | None,
        Field(
            description=(
                "Optional single WGEA reporting year to restrict the ranking "
                "to. Format: 'YYYY-YY' (e.g. '2024-25') or 'YYYY' (e.g. "
                "'2024'). Defaults to the latest reporting year present in "
                "the data so the rank is a clean 'top N at the current "
                "reporting year' view."
            ),
            examples=["2024-25", "2023-24", "2024", 2024],
        ),
    ] = None,
) -> DataResponse:
    """Return the N rows with the largest (or smallest) value of a measure.

    Ranks across one WGEA reporting year (the latest by default, or a
    specific year via `reporting_year=`). This is the most common agent
    workflow — "show me the top 10 X by Y" — collapsed into a single
    server-side call: rank-and-slice happens on the server so the agent
    never has to fetch a full table just to take the top of it.

    Examples:
        # 10 employers with the most women managers (latest reporting year)
        top_n("WORKFORCE_COMPOSITION", "n_employees", n=10,
              filters={"gender": "Women", "manager_category": "Manager"})

        # 5 ANZSIC divisions with the fewest Yes responses on Gender Pay Gap
        top_n("GENDER_EQUALITY_ACTIONS", "n_responses", n=5, direction="bottom",
              filters={"section": "Gender Pay Gap", "response": "Yes"})

        # Top 5 employers in Mining by total workforce in 2023-24
        top_n("WORKFORCE_COMPOSITION", "n_employees", n=5,
              filters={"anzsic_division": "Mining"},
              reporting_year="2023-24")

    Returns:
        DataResponse with at most `n` records, sorted by `measure` value
        in the requested direction. Other fields (reporting_year, unit,
        attribution) match a regular get_data call.
    """
    # Validate inputs that pydantic's runtime can't enforce strictly when
    # the function is called directly (Literal/ge/le are type-checker-only
    # in some FastMCP code paths).
    if not isinstance(measure, str) or not measure.strip():
        raise ValueError(
            "measure is required and must be a non-empty string. "
            "Example: top_n('WORKFORCE_COMPOSITION', 'n_employees', n=10). "
            "Try describe_dataset(<id>) to see available measure keys."
        )
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(
            f"n must be a positive integer (1-100), got {n!r} ({type(n).__name__}). "
            "Try n=10 (default) or n=5. Valid range: 1-100."
        )
    if n < 1:
        raise ValueError(
            f"n must be >= 1, got {n}. "
            "Try n=10 (default) or n=5. Valid range: 1-100."
        )
    if n > 100:
        raise ValueError(
            f"n must be <= 100, got {n}. "
            "Try n=10 (default) or n=50. Valid range: 1-100."
        )
    if direction not in ("top", "bottom"):
        valid = ["top", "bottom"]
        suggestion = _fuzzy_suggest(str(direction).lower(), valid, cutoff=60)
        suggest_msg = f"Did you mean {suggestion!r}? " if suggestion else ""
        raise ValueError(
            f"direction must be 'top' or 'bottom', got {direction!r}. "
            f"{suggest_msg}"
            "Try direction='top' (default, largest values first) or "
            "direction='bottom' (smallest values first)."
        )

    norm_id = _normalize_dataset_id(dataset_id)
    cd = curated.get(norm_id)
    if cd is None:
        raise ValueError(_unknown_dataset_msg(dataset_id))
    measure_key = measure.strip()
    measure_keys = [c.key for c in curated.measure_columns(cd)]
    if measure_key not in measure_keys:
        suggestion = _fuzzy_suggest(measure_key, measure_keys, cutoff=60)
        suggest_msg = f"Did you mean {suggestion!r}? " if suggestion else ""
        raise ValueError(
            f"Unknown measure {measure!r} for dataset {norm_id!r}. "
            f"{suggest_msg}"
            f"Valid measures: {', '.join(sorted(measure_keys))}. "
            f"See describe_dataset({norm_id!r}) for the full schema."
        )

    # Validate `reporting_year` separately from start_period/end_period — it
    # uses the same WGEA YYYY-YY / YYYY shape but is treated as a single-year
    # restriction (both bounds set to this value).
    year_v = _validate_period(reporting_year, "reporting_year")

    # Pull all rows for the dataset (optionally filtered by reporting_year),
    # then rank server-side. Setting both start and end to the same year is
    # the WGEA-canonical way to slice to a single reporting year.
    if year_v is not None:
        start_v: str | None = year_v
        end_v: str | None = year_v
        latest_only_flag = False
    else:
        # Default: latest reporting year only (cheap-cache discovery path
        # already picks the freshest year — match its behaviour).
        start_v = None
        end_v = None
        latest_only_flag = True

    full = await _get_data_impl(
        norm_id,
        filters,
        start_v,
        end_v,
        "records",
        max_rows=_HARD_MAX_ROWS,
        measures=measure_key,
        latest_only=latest_only_flag,
    )
    valid_records = [
        r for r in full.records
        if getattr(r, "value", None) is not None
    ]
    valid_records.sort(
        key=lambda r: r.value,  # type: ignore[union-attr,return-value]
        reverse=(direction == "top"),
    )
    top = valid_records[:n]
    # Preserve the response envelope; replace records and row_count.
    return full.model_copy(update={"records": top, "row_count": len(top)})


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
