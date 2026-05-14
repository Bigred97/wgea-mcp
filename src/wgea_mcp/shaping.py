"""Translate a parsed DataFrame into the public DataResponse shape.

WGEA datasets come in two shapes:

  1. **Wide / count datasets** — workforce_composition + workforce_management.
     One numeric measure (`n_employees`) with many dimensions
     (employer, occupation, manager_category, gender, etc.). Standard wide
     long-format layout.

  2. **Long / questionnaire datasets** — gender_equality_actions,
     parental_leave_flex, harm_prevention, employee_support, workplace_overview.
     `response` is a string-valued column captured in dimensions; the
     numeric "measure" is a count (`n_responses` = 1 per matched row) used
     for aggregation.

The shaping layer:
  1. Renames source columns to plain-English aliases per the curated YAML.
  2. Coerces dtypes (string IDs cleaned, numeric measures → float).
  3. Filters by user-supplied dimension values (including fuzzy employer-name
     match via rapidfuzz when the filter targets a permissive column).
  4. Filters by reporting_year range when start_period / end_period are set.
  5. Emits an Observation per (row × measure) cell.

The result is uniform across every curated dataset — same Observation shape
every time.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import pandas as pd

from .curated import (
    CuratedColumn,
    CuratedDataset,
    dimension_columns,
    id_columns,
    measure_columns,
    resolve_measure_keys,
    translate_filter_value,
)
from .models import DataResponse, Observation


# Fuzzy-match threshold for employer-name search via the combined scorer.
# The combined scorer averages WRatio + partial_ratio so typos like
# "Commonweath Bank" → "Commonwealth Bank Of Australia" rank above
# unrelated "Bank"-containing names like "Bendigo And Adelaide Bank Limited"
# (which WRatio alone ties at 85.5 with Commonwealth, producing non-
# deterministic ordering).
_EMPLOYER_FUZZY_THRESHOLD = 75


def _employer_scorer(s1, s2, **kwargs):
    """Combined WRatio + partial_ratio scorer for employer-name fuzzy match.

    WRatio alone ties typos like "Commonweath Bank" between Commonwealth and
    Bendigo (both have "Bank"). partial_ratio breaks the tie decisively
    in favour of the substring match (Commonwealth has the longer matching
    substring against the typo).
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover — rapidfuzz is a required dep
        return 0.0
    return (fuzz.WRatio(s1, s2, **kwargs) + fuzz.partial_ratio(s1, s2, **kwargs)) / 2

# Static alias map for top Australian employers. Loaded once. Aliases like
# "CBA" → "commonwealth bank of australia" are checked BEFORE rapidfuzz so
# common abbreviations resolve deterministically. After the alias lookup we
# substring-match the canonical phrase against the dataset's employer_name
# column (which carries WGEA's verbose legal names).
_ALIAS_CACHE: dict[str, str] | None = None


def _load_employer_aliases() -> dict[str, str]:
    """Lower-cased alias → canonical-substring lookup."""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    text: str | None = None
    try:
        ref = resources.files("wgea_mcp").joinpath("data/employer_aliases.json")
        text = ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        here = Path(__file__).resolve().parent / "data" / "employer_aliases.json"
        if here.is_file():
            text = here.read_text(encoding="utf-8")
    if not text:
        _ALIAS_CACHE = {}
        return _ALIAS_CACHE
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        _ALIAS_CACHE = {}
        return _ALIAS_CACHE
    raw = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        _ALIAS_CACHE = {}
        return _ALIAS_CACHE
    _ALIAS_CACHE = {
        str(k).strip().lower(): str(v).strip().lower() for k, v in raw.items()
    }
    return _ALIAS_CACHE


def reset_alias_cache_for_tests() -> None:
    global _ALIAS_CACHE
    _ALIAS_CACHE = None


def _safe_value(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _safe_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return str(v)


def _apply_aliases(df: pd.DataFrame, cd: CuratedDataset) -> pd.DataFrame:
    """Rename source columns to their curated aliases.

    Skips renames where the curated column key already equals the source
    column name (very common in WGEA — most columns keep their native names).
    Raises if any expected source column is missing — that signals a WGEA
    schema change.
    """
    rename_map: dict[str, str] = {}
    measure_source_seen: set[str] = set()
    for col in cd.columns.values():
        if col.source_column == col.key:
            continue  # no rename needed
        if col.source_column in df.columns:
            # If the same source column maps to multiple aliases (e.g. response
            # → both a dimension and a count measure), only rename once and
            # copy for the others below.
            if col.source_column in rename_map.values():
                continue
            rename_map[col.source_column] = col.key
            measure_source_seen.add(col.source_column)

    missing = [
        c.source_column
        for c in cd.columns.values()
        if c.source_column not in df.columns
    ]
    if missing:
        sample_cols = list(df.columns)[:6]
        raise ValueError(
            f"Dataset {cd.id!r} expected these columns but they were not in "
            f"the parsed table: {missing[:5]}{'...' if len(missing) > 5 else ''}. "
            f"Saw these column headers instead (first 6): {sample_cols}. "
            "The upstream file may have changed shape — flag at "
            "https://github.com/Bigred97/wgea-mcp/issues."
        )

    out = df.rename(columns=rename_map)

    # Duplicate columns for cases where the same source column is exposed as
    # multiple curated columns (e.g. `response` is both a dimension and the
    # source for the `n_responses` measure).
    for col in cd.columns.values():
        if col.key not in out.columns and col.source_column in out.columns:
            out[col.key] = out[col.source_column]
        if col.key not in out.columns and col.source_column in df.columns:
            out[col.key] = df[col.source_column]

    keep = [c.key for c in cd.columns.values() if c.key in out.columns]
    return out[keep].copy()


def _coerce_dtypes(df: pd.DataFrame, cd: CuratedDataset) -> pd.DataFrame:
    for col in cd.columns.values():
        if col.dtype and col.key in df.columns:
            try:
                if col.dtype in ("int", "integer"):
                    df[col.key] = pd.to_numeric(df[col.key], errors="coerce").astype(
                        "Int64"
                    )
                elif col.dtype in ("float", "number"):
                    df[col.key] = pd.to_numeric(df[col.key], errors="coerce")
                elif col.dtype in ("string", "str"):
                    df[col.key] = _to_clean_string(df[col.key])
                elif col.dtype == "count":
                    # Synthetic count measure — every non-null row contributes 1.
                    df[col.key] = (~df[col.key].isna()).astype("Int64")
            except (ValueError, TypeError):
                pass
    return df


def _to_clean_string(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        rounded = series.dropna()
        if not rounded.empty and (rounded.astype("float64") % 1 == 0).all():
            return series.astype("Int64").astype("string")
    return series.astype("string").str.strip()


def fuzzy_match_employer(
    df: pd.DataFrame,
    column: str,
    user_value: str,
    threshold: int = _EMPLOYER_FUZZY_THRESHOLD,
) -> tuple[list[str], list[str]]:
    """Return (matched_values, did_you_mean_suggestions).

    Strategy:
      1. Static alias lookup — "CBA" → "commonwealth bank of australia".
      2. Exact case-insensitive match against the column.
      3. Substring match against the column.
      4. RapidFuzz WRatio ≥ threshold → return matches + top-5 "did you mean".
      5. No match → empty matches + top-5 hints.

    The "did you mean" list is filtered to only show suggestions with score
    above 50, so noise from completely-unrelated matches is suppressed.
    """
    try:
        from rapidfuzz import fuzz, process
    except ImportError:  # pragma: no cover — rapidfuzz is a required dep
        return [], []

    if column not in df.columns or not user_value:
        return [], []

    series = df[column].astype("string").dropna().drop_duplicates()
    haystack = series.tolist()
    if not haystack:
        return [], []

    needle = user_value.strip()
    needle_l = needle.lower()

    # Tier 1: static alias expansion. Resolves "CBA" → canonical substring,
    # then re-runs the substring/exact tiers against the column.
    aliases = _load_employer_aliases()
    canonical = aliases.get(needle_l)
    if canonical:
        substrings = [v for v in haystack if canonical in v.lower()]
        if substrings:
            return substrings[:50], []

    # Tier 2: exact case-insensitive match
    exact = [v for v in haystack if v.lower() == needle_l]
    if exact:
        return exact, []

    # Tier 3: substring match
    substrings = [v for v in haystack if needle_l in v.lower()]
    if substrings:
        return substrings[:50], []

    # Tier 4: combined rapidfuzz scorer fallback (handles typos)
    matches = process.extract(needle, haystack, scorer=_employer_scorer, limit=10)
    accepted = [m[0] for m in matches if m[1] >= threshold]
    suggestions = [m[0] for m in matches[:5] if m[1] >= 50]
    if accepted:
        return accepted, suggestions
    return [], suggestions


def _apply_filters(
    df: pd.DataFrame,
    cd: CuratedDataset,
    filters: dict[str, Any],
) -> tuple[pd.DataFrame, list[str]]:
    """Filter rows by user-supplied dimension values.

    Permissive dimensions accept any value the user supplies. For the
    `employer_name` (and `corporate_group_name`) columns we additionally
    run rapidfuzz to upgrade approximate matches into hard matches and
    surface "did you mean?" hints when nothing matches exactly.

    Returns (filtered_df, did_you_mean_suggestions).
    """
    if not filters:
        return df, []

    valid_dim_keys = {
        c.key for c in cd.columns.values() if c.role in ("dimension", "id")
    }
    out = df
    suggestions: list[str] = []
    for user_key, user_val in filters.items():
        if user_key not in valid_dim_keys:
            valid = sorted(valid_dim_keys)
            raise ValueError(
                f"Unknown filter {user_key!r} for dataset {cd.id!r}. "
                f"Try one of: {', '.join(valid[:15])}"
            )
        if user_val is None:
            raise ValueError(
                f"Filter {user_key!r} value is None. Pass a string, number, or list "
                "of strings — or omit the filter entirely to disable it."
            )
        col_def = cd.columns.get(user_key)
        permissive_col = bool(col_def and col_def.permissive)
        fuzzy_col = user_key in ("employer_name", "corporate_group_name")

        if isinstance(user_val, list):
            if not user_val:
                raise ValueError(
                    f"Filter {user_key!r} has an empty list. "
                    "Pass at least one value, or omit the filter."
                )
            # Reject None list entries early so they can't bleed into str() coercion.
            for v in user_val:
                if v is None:
                    raise ValueError(
                        f"Filter {user_key!r} list contains None. "
                        "Drop the None entry or omit the filter."
                    )
            if fuzzy_col:
                # Expand each list item via the alias map / fuzzy matcher so
                # ['CBA', 'NAB'] resolves to {'Commonwealth Bank Of Australia',
                # 'National Australia Bank Limited'} etc.
                resolved: list[str] = []
                for v in user_val:
                    matched_values, dym = fuzzy_match_employer(
                        out, user_key, str(v).strip()
                    )
                    if matched_values:
                        resolved.extend(matched_values)
                    else:
                        suggestions.extend(dym)
                if resolved:
                    mask = out[user_key].astype("string").isin(resolved)
                else:
                    mask = pd.Series([False] * len(out), index=out.index)
            else:
                resolved = [
                    translate_filter_value(cd, user_key, str(v).strip())
                    for v in user_val
                ]
                mask = out[user_key].astype("string").isin(resolved)
        else:
            v_str = str(user_val).strip()
            # Wildcard substring match: 'cba*' or '*cba*' or 'cba~'
            if permissive_col and (
                v_str.endswith("*") or v_str.startswith("*") or "~" in v_str
            ):
                needle = v_str.replace("*", "").replace("~", "").strip()
                if not needle:
                    raise ValueError(
                        f"Filter {user_key!r}: wildcard value reduced to empty after stripping '*'."
                    )
                mask = (
                    out[user_key]
                    .astype("string")
                    .str.contains(needle, case=False, na=False, regex=False)
                )
            elif fuzzy_col:
                matched_values, dym = fuzzy_match_employer(out, user_key, v_str)
                if matched_values:
                    mask = out[user_key].astype("string").isin(matched_values)
                else:
                    suggestions.extend(dym)
                    # No matches — return an empty slice but keep the hints.
                    mask = pd.Series([False] * len(out), index=out.index)
            else:
                resolved_single = translate_filter_value(cd, user_key, v_str)
                mask = out[user_key].astype("string") == str(resolved_single)
        out = out.loc[mask]
    return out.reset_index(drop=True), suggestions


def _apply_period_range(
    df: pd.DataFrame,
    cd: CuratedDataset,
    start_period: str | None,
    end_period: str | None,
) -> pd.DataFrame:
    """Filter rows by start_period / end_period against cd.period_column.

    WGEA `reporting_year` is a string like "2024-25" — lexical comparison
    over these labels works correctly ("2023-24" < "2024-25" < "2025-26").
    Accepted user inputs:
      - "YYYY-YY"   → passthrough ("2024-25")
      - "YYYY"      → expand to "(YYYY-1)-YY" for start, "YYYY-YY" for end
    """
    if not cd.period_column or not (start_period or end_period):
        return df
    if cd.period_column not in df.columns:
        return df
    series = df[cd.period_column].astype("string")
    if start_period:
        norm = _expand_period_input(start_period, bound="start")
        df = df.loc[series >= norm]
        series = df[cd.period_column].astype("string")
    if end_period:
        norm = _expand_period_input(end_period, bound="end")
        df = df.loc[series <= norm]
    return df.reset_index(drop=True)


def _expand_period_input(value: str, *, bound: str) -> str:
    """Expand a user-supplied period string to a WGEA reporting-year label.

    Accepts:
      - "YYYY-YY" → passthrough ("2024-25")
      - "YYYY"    → "(YYYY-1)-YY" if start, "YYYY-YY" if end
      - anything else → passthrough (best-effort lexical compare)
    """
    if not value:
        return value
    s = value.strip()
    if len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit():
        return s
    if len(s) == 4 and s.isdigit():
        year = int(s)
        if bound == "start":
            return f"{year - 1}-{str(year)[-2:]}"
        return f"{year - 1}-{str(year)[-2:]}"
    return s


def shape_wide(
    df: pd.DataFrame,
    cd: CuratedDataset,
    measures: list[str],
) -> list[Observation]:
    """One Observation per (row, measure) cell."""
    if df.empty:
        return []
    period_alias = cd.period_column if cd.period_column in df.columns else None

    dims = [c.key for c in dimension_columns(cd) if c.key != period_alias]
    ids = [c.key for c in id_columns(cd)]
    dim_keys = dims + ids
    measure_by_key = {c.key: c for c in measure_columns(cd)}

    records: list[Observation] = []
    for _, row in df.iterrows():
        dim_vals: dict[str, Any] = {}
        for k in dim_keys:
            if k in df.columns:
                v = _safe_str(row[k])
                if v is not None:
                    dim_vals[k] = v
        period_val = (
            _safe_str(row[period_alias]) if period_alias and period_alias in df.columns else None
        )
        for mk in measures:
            mc = measure_by_key.get(mk)
            if mc is None:
                continue
            cell = row[mk] if mk in df.columns else None
            value = _safe_value(cell)
            if value is None:
                continue
            records.append(
                Observation(
                    reporting_year=period_val,
                    value=value,
                    measure=mk,
                    dimensions=dim_vals,
                    unit=mc.unit,
                )
            )
    return records


def records_to_csv(records: list[Observation]) -> str:
    if not records:
        return ""
    dim_keys: list[str] = []
    seen: set[str] = set()
    for r in records:
        for k in r.dimensions:
            if k not in seen:
                seen.add(k)
                dim_keys.append(k)
    cols = ["reporting_year", "measure", "value", "unit", *dim_keys]
    df = pd.DataFrame(
        [
            {
                "reporting_year": r.reporting_year,
                "measure": r.measure,
                "value": r.value,
                "unit": r.unit,
                **{k: r.dimensions.get(k) for k in dim_keys},
            }
            for r in records
        ],
        columns=cols,
    )
    return df.to_csv(index=False)


def records_to_series(records: list[Observation]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for r in records:
        key = r.measure or "value"
        g = groups.setdefault(
            key, {"measure": key, "unit": r.unit, "observations": []}
        )
        g["observations"].append(
            {
                "reporting_year": r.reporting_year,
                "value": r.value,
                "dimensions": r.dimensions,
            }
        )
    return list(groups.values())


def build_response(
    *,
    cd: CuratedDataset,
    df: pd.DataFrame,
    filters: dict[str, Any],
    measures: str | list[str] | None,
    start_period: str | None,
    end_period: str | None,
    fmt: str,
    user_query: dict[str, Any],
    download_url: str | None = None,
    source_url: str | None = None,
    stale: bool = False,
    stale_reason: str | None = None,
    max_rows: int | None = None,
) -> DataResponse:
    """Single entrypoint shaping uses to build a DataResponse."""
    if df is None or df.empty:
        return DataResponse(
            dataset_id=cd.id,
            dataset_name=cd.name,
            query=user_query,
            row_count=0,
            records=[],
            csv="" if fmt == "csv" else None,
            retrieved_at=datetime.now(timezone.utc),
            source_url=source_url or cd.source_url,
            download_url=download_url,
            stale=stale,
            stale_reason=stale_reason,
        )
    renamed = _apply_aliases(df, cd)
    coerced = _coerce_dtypes(renamed, cd)
    filtered, suggestions = _apply_filters(coerced, cd, filters)
    period_filtered = _apply_period_range(filtered, cd, start_period, end_period)

    measure_keys = resolve_measure_keys(cd, measures)

    # Track the pre-truncation count so we can surface it on DataResponse.
    # The trust contract (CLAUDE.md dim #2 — Data Pruning) requires that when
    # max_rows truncates a larger post-filter result we tell the agent how
    # many rows were dropped so it can decide whether to paginate or tighten
    # filters. None means "no truncation happened".
    pre_truncation_rows = len(period_filtered)
    truncated_at: int | None = None
    if max_rows is not None and max_rows > 0 and pre_truncation_rows > max_rows:
        period_filtered = period_filtered.iloc[:max_rows].reset_index(drop=True)
        truncated_at = pre_truncation_rows

    records = shape_wide(period_filtered, cd, measure_keys)

    response_unit: str | None = None
    if records:
        units = {r.unit for r in records if r.unit}
        if len(units) == 1:
            response_unit = next(iter(units))

    reporting_year_latest: str | None = None
    if records:
        years = sorted({r.reporting_year for r in records if r.reporting_year})
        if years:
            reporting_year_latest = years[-1]

    if fmt == "csv":
        out_records: list[Observation] | list[dict[str, Any]] = []
        csv_text: str | None = records_to_csv(records)
    elif fmt == "series":
        out_records = records_to_series(records)
        csv_text = None
    else:
        out_records = records
        csv_text = None

    return DataResponse(
        dataset_id=cd.id,
        dataset_name=cd.name,
        query=user_query,
        reporting_year=reporting_year_latest,
        unit=response_unit,
        row_count=len(records),
        records=out_records,
        csv=csv_text,
        retrieved_at=datetime.now(timezone.utc),
        source_url=source_url or cd.source_url,
        download_url=download_url,
        did_you_mean=suggestions,
        stale=stale,
        stale_reason=stale_reason,
        truncated_at=truncated_at,
    )
