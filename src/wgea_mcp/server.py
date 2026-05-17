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
from .parsing import (
    drop_blank_rows,
    parse_egpg_xlsx,
    read_csv_from_zip,
    stream_csv_from_zip,
)
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

# Datasets for which we apply the streaming/early-exit fast path on cold
# calls with a `max_rows` cap. These are the four whose full-parse cost
# was customer-blocking the hosted API (workforce_composition: 56 MB / 211k
# rows / ~5s parse; employee_support: 154 MB / 332k rows / ~13s parse;
# gender_equality_actions: 77 MB / hundreds of thousands of rows;
# workforce_management: 235k rows). The other 3 WGEA CSVs are unaffected —
# they keep the existing full-parse + df-cache path so warm repeat queries
# stay sub-50ms.
_STREAMING_DATASETS = frozenset({
    "WORKFORCE_COMPOSITION",
    "EMPLOYEE_SUPPORT",
    "GENDER_EQUALITY_ACTIONS",
    "WORKFORCE_MANAGEMENT",
})


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
        "Enumerate the curated set or search by keyword to discover IDs."
    )


def _normalize_dataset_id(dataset_id: Any) -> str:
    if not isinstance(dataset_id, str):
        raise ValueError(
            f"dataset_id must be a string, got {type(dataset_id).__name__}. "
            "Search by keyword or enumerate the curated set to discover IDs."
        )
    norm = dataset_id.strip().upper()
    if not norm:
        raise ValueError(
            "dataset_id is empty. Enumerate the curated set to see available IDs."
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


async def _fetch_and_parse_xlsx(
    cd: curated.CuratedDataset,
) -> tuple[pd.DataFrame, str, str, bool, str | None]:
    """Fetch the EGPG xlsx + aggregate to the HEADLINE_GAP DataFrame.

    The xlsx_aggregated path skips CKAN discovery — the WGEA spreadsheet
    URL is stable (`Employer-Gender-Pay-Gaps-Spreadsheet.xlsx`) and the
    file is hosted on wgea.gov.au, not data.gov.au. Reporting year is
    pulled from the title row inside the xlsx, so it stays in sync with
    WGEA's release cadence without a separate discovery call.

    Cached DataFrame is keyed by (url, dataset_id, body_size, sha256-of-
    head-and-tail) so a fresh WGEA upload invalidates the cache even if
    the URL is unchanged.

    Returns (df, url, reporting_year_label, stale, stale_reason) — same
    shape as `_fetch_and_parse` so the dispatcher can swap freely.
    """
    if not cd.download_url:
        # Defensive — curated.py rejects xlsx_aggregated YAMLs without a
        # download_url, so this branch is unreachable in practice.
        raise ValueError(
            f"Dataset {cd.id!r} is xlsx_aggregated but its curated entry "
            "is missing a download_url."
        )
    client = await _get_client()
    try:
        body = await client.fetch_resource(cd.download_url, kind="data")
    except WGEAAPIError as e:
        raise ValueError(
            f"Could not fetch WGEA spreadsheet for {cd.id!r}. ({e})"
        ) from e

    head = body[:8192]
    tail = body[-2048:] if len(body) > 8192 else b""
    body_sig = hashlib.sha256(head + tail).digest()
    cache_key = (cd.download_url, cd.id, len(body), body_sig)

    async with _df_cache_lock:
        cached = _df_cache.get(cache_key)
        if cached is not None:
            _df_cache.move_to_end(cache_key)
            # Reporting year is the first row of the cached DataFrame —
            # all HEADLINE_GAP rows share the same year.
            year_label = _year_label_from_df(cached)
            return cached, cd.download_url, year_label, False, None

    # Parse off-loop — openpyxl is sync + slow on ~2 MB xlsx.
    df, year_label = await asyncio.to_thread(parse_egpg_xlsx, body)

    async with _df_cache_lock:
        _df_cache[cache_key] = df
        _df_cache.move_to_end(cache_key)
        while len(_df_cache) > _DF_CACHE_MAX_ENTRIES:
            _df_cache.popitem(last=False)
    return df, cd.download_url, year_label, False, None


def _year_label_from_df(df: pd.DataFrame) -> str:
    """Pull the reporting_year string out of a parsed HEADLINE_GAP DataFrame.

    All rows share the same year — safely return the first non-null value.
    Empty / missing column falls back to empty string (the warm-cache path
    only enters here after `parse_egpg_xlsx` already populated the column).
    """
    if "reporting_year" not in df.columns or df.empty:
        return ""
    series = df["reporting_year"].dropna()
    return str(series.iloc[0]) if not series.empty else ""


async def _fetch_zip_body(
    cd: curated.CuratedDataset,
) -> tuple[bytes, str, str, bool, str | None, tuple]:
    """Resolve URL, fetch ZIP bytes. Returns (body, url, year_label, stale,
    stale_reason, df_cache_key). Network + discovery only — no CSV parse."""
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
    return (
        body,
        resolved.url,
        resolved.reporting_year_label,
        resolved.stale,
        resolved.reason,
        cache_key,
    )


async def _fetch_and_parse(
    cd: curated.CuratedDataset,
) -> tuple[pd.DataFrame, str, str, bool, str | None]:
    """Resolve URL, fetch ZIP bytes, extract CSV, parse to full DataFrame.

    Returns (df, zip_url, reporting_year_label, stale, stale_reason).
    Caches the parsed DataFrame so subsequent warm calls (irrespective of
    filters) return in ~50ms.
    """
    body, url, year_label, stale, stale_reason, cache_key = await _fetch_zip_body(cd)

    async with _df_cache_lock:
        cached = _df_cache.get(cache_key)
        if cached is not None:
            _df_cache.move_to_end(cache_key)
            return (
                cached,
                url,
                year_label,
                stale,
                stale_reason,
            )

    # Run sync zipfile + pandas parse off the event loop. WGEA's annual
    # ZIP is ~71MB containing 7 thematic CSVs (largest ~160MB unzipped);
    # parsing inline blocks the async tool for seconds, serialises
    # concurrent requests, and stalls downstream consumers like the
    # ausdata-api gateway.
    df = await asyncio.to_thread(read_csv_from_zip, body, cd.zip_member)

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
        url,
        year_label,
        stale,
        stale_reason,
    )


def _can_pushdown_filters(cd: curated.CuratedDataset, filters: dict[str, Any]) -> bool:
    """True if every filter is a simple equality match on a non-fuzzy column.

    The streaming fast path skips rapidfuzz / wildcard / list-with-alias
    expansion so it doesn't ship correctness regressions on the warm path.
    For complex filters we fall back to the full-parse path (still cached
    after the first call) — those filter shapes are rare on the customer-
    blocking limit=2 / limit=50 calls this fix is targeting.
    """
    if not filters:
        return True
    valid_dim_keys = {
        c.key for c in cd.columns.values() if c.role in ("dimension", "id")
    }
    for user_key, user_val in filters.items():
        if user_key not in valid_dim_keys:
            return False
        # Fuzzy columns must run through rapidfuzz against the full set of
        # distinct employer names — can't push that down to row iteration.
        if user_key in ("employer_name", "corporate_group_name"):
            return False
        if user_val is None:
            return False
        if isinstance(user_val, list):
            # Multi-value lists are translated row-side; safe to push down
            # if no entry triggers wildcard / alias logic.
            for v in user_val:
                if v is None:
                    return False
                v_str = str(v).strip()
                if "*" in v_str or "~" in v_str:
                    return False
        else:
            v_str = str(user_val).strip()
            if "*" in v_str or "~" in v_str:
                return False
    return True


def _build_row_predicate(
    cd: curated.CuratedDataset,
    filters: dict[str, Any],
    period_column: str | None,
    period_value: str | None,
    start_period: str | None,
    end_period: str | None,
) -> Any:
    """Return a row_predicate(dict) -> bool for stream_csv_from_zip.

    Filters are translated through `curated.translate_filter_value` so user
    aliases ('women' -> 'Women') still resolve. Period filtering is applied
    using lexical string compare against `period_column` (WGEA reporting-year
    labels sort correctly as strings). Returns None when there are no
    predicates to enforce (fastest path: just take the first max_rows rows).
    """
    # Translate each filter to a set of source-column accepted values.
    # The streaming parser sees raw string cell values, so we compare strings.
    expected: dict[str, set[str]] = {}
    for user_key, user_val in (filters or {}).items():
        col_def = cd.columns.get(user_key)
        if col_def is None:
            # Defensive: caller should have filtered these out via _can_pushdown_filters.
            return _PREDICATE_REJECT_ALL
        source_col = col_def.source_column
        if isinstance(user_val, list):
            translated = {
                curated.translate_filter_value(cd, user_key, str(v).strip())
                for v in user_val
            }
        else:
            translated = {
                curated.translate_filter_value(cd, user_key, str(user_val).strip())
            }
        # Store as strings; compare to the raw CSV cell value (also a string).
        expected[source_col] = {str(v) for v in translated}

    do_period_filter = bool(
        period_column and (period_value or start_period or end_period)
    )
    if not expected and not do_period_filter:
        return None

    def predicate(row: dict[str, str]) -> bool:
        if do_period_filter:
            cell = row.get(period_column, "")
            cell_s = cell if isinstance(cell, str) else "" if cell is None else str(cell)
            if period_value is not None and cell_s != period_value:
                return False
            if start_period is not None and cell_s < start_period:
                return False
            if end_period is not None and cell_s > end_period:
                return False
        for source_col, accepted in expected.items():
            cell = row.get(source_col, "")
            cell_s = cell if isinstance(cell, str) else "" if cell is None else str(cell)
            if cell_s not in accepted:
                return False
        return True

    return predicate


def _PREDICATE_REJECT_ALL(_row: dict[str, str]) -> bool:  # noqa: N802
    return False


async def _fetch_and_parse_streaming(
    cd: curated.CuratedDataset,
    *,
    max_rows: int,
    filters: dict[str, Any],
    start_period: str | None,
    end_period: str | None,
    latest_only: bool,
) -> tuple[pd.DataFrame, str, str, bool, str | None]:
    """Fast-path fetch+parse for the largest CSVs: stream rows, push down
    `max_rows` and equality filters, short-circuit on cap.

    Returns the same (df, url, year_label, stale, stale_reason) shape as
    `_fetch_and_parse`. The DataFrame has source-column headers (renaming
    to plain-English aliases happens in shaping.build_response).

    Bypasses the parsed-DataFrame cache: the result depends on filters and
    is request-specific. The ZIP byte cache (SQLite) and the in-process
    body LRU still amortise the ~71 MB network fetch across calls.
    """
    body, url, year_label, stale, stale_reason, cache_key = await _fetch_zip_body(cd)

    # If the warm DF cache already has the full parse, skip streaming — at
    # this point an in-memory pandas .loc slice is faster than reparsing
    # the CSV bytes. The caller (`_get_data_impl`) only enters this branch
    # when the cache miss is the bottleneck.
    async with _df_cache_lock:
        cached = _df_cache.get(cache_key)
        if cached is not None:
            _df_cache.move_to_end(cache_key)
            return (cached, url, year_label, stale, stale_reason)

    period_value = year_label if latest_only else None
    predicate = _build_row_predicate(
        cd,
        filters,
        cd.period_column,
        period_value,
        start_period,
        end_period,
    )
    # Read +1 so build_response can populate truncated_at when the post-filter
    # population is larger than max_rows (lets agents detect the cap was hit).
    streaming_cap = max_rows + 1
    df = stream_csv_from_zip(
        body,
        cd.zip_member,
        max_rows=streaming_cap,
        row_predicate=predicate,
    )

    dim_source_cols = [
        c.source_column for c in cd.columns.values() if c.role == "dimension"
    ]
    if dim_source_cols:
        df = drop_blank_rows(df, dim_source_cols)

    return (df, url, year_label, stale, stale_reason)


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

    # Try a cheap fetch to surface the current reporting year — non-fatal.
    # xlsx_aggregated datasets (HEADLINE_GAP) carry the year in their xlsx
    # title row, not on data.gov.au — fetch the spreadsheet's parsed
    # DataFrame to read it (warm-cached after the first request).
    year_label: str | None = None
    try:
        if cd.format == "xlsx_aggregated":
            df, _url, year_label, _stale, _reason = await _fetch_and_parse_xlsx(cd)
        else:
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

    user_query: dict[str, Any] = {}
    if filters_d:
        user_query["filters"] = dict(filters_d)
    if measures is not None:
        user_query["measures"] = measures
    if start_v:
        user_query["start_period"] = start_v
    if end_v:
        user_query["end_period"] = end_v

    # Dispatch on dataset format:
    # - xlsx_aggregated (HEADLINE_GAP) — fetch the WGEA spreadsheet, run
    #   the in-memory aggregator. No CKAN discovery; reporting_year comes
    #   from the xlsx title row. Result is ~20 rows so no streaming path.
    # - csv_in_zip — the 7 questionnaire / composition datasets. Use the
    #   streaming fast path for the largest CSVs when filters are simple
    #   equality (cuts ~5-13s off the cold-call). Otherwise full-parse.
    if cd.format == "xlsx_aggregated":
        df, url_used, year_label, stale, stale_reason = await _fetch_and_parse_xlsx(cd)
        # HEADLINE_GAP only carries one reporting year per WGEA release, so
        # latest_only is a no-op — every row already matches the resolved
        # year. Skip the slice rather than triggering an unnecessary scan.
    elif (
        cd.id in _STREAMING_DATASETS
        and _can_pushdown_filters(cd, filters_d)
    ):
        df, url_used, year_label, stale, stale_reason = (
            await _fetch_and_parse_streaming(
                cd,
                max_rows=effective_max,
                filters=filters_d,
                start_period=start_v,
                end_period=end_v,
                latest_only=latest_only,
            )
        )
    else:
        df, url_used, year_label, stale, stale_reason = await _fetch_and_parse(cd)
        # If latest_only, restrict to just the resolved reporting year.
        if latest_only:
            if cd.period_column in df.columns:
                df = df.loc[df[cd.period_column].astype("string") == year_label]
                df = df.reset_index(drop=True)
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
            "Use the describe endpoint or describe tool to see the available measure keys."
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
            f"Use the describe endpoint or describe tool to see the full schema for {norm_id!r}."
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
