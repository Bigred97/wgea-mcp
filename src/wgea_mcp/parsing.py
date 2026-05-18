"""CSV-in-ZIP parsers for the WGEA Public Data File.

WGEA's annual ZIP unpacks to 8 thematic CSVs:
  - wgea_workforce_composition_<YEAR>.csv
  - wgea_workforce_management_statistics_<YEAR>.csv
  - wgea_questionnaire_action_on_gender_equality_<YEAR>.csv
  - wgea_questionnaire_employee_support_<YEAR>.csv
  - wgea_questionnaire_flexible_work_<YEAR>.csv
  - wgea_questionnaire_harm_prevention_<YEAR>.csv
  - wgea_questionnaire_workplace_overview_<YEAR>.csv
  - wgea_questionnaire_catalogue_<YEAR>.csv      (metadata, not exposed)

`read_csv_from_zip(zip_bytes, member_pattern)` extracts the matching CSV
without unpacking the whole archive, parses it via pandas, and returns
a DataFrame. The member_pattern is a regex applied to filenames inside
the ZIP — this means curated YAMLs can declare a year-agnostic pattern
("wgea_workforce_composition_") that resolves to the actual year-suffixed
filename at runtime.

`stream_csv_from_zip(...)` is the limit-pushed-down variant: it iterates
rows with `csv.reader` and stops as soon as `max_rows` matching rows have
been collected. Used for the two largest CSVs (workforce_composition,
employee_support) where the full pandas parse is the timeout cost a
`limit=2`-style query was paying for.
"""
from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from collections.abc import Callable
from io import BytesIO
from typing import Any

import pandas as pd


class ParseError(Exception):
    """Raised when a WGEA resource can't be parsed."""


def list_zip_members(zip_bytes: bytes) -> list[str]:
    """Return all filenames inside a WGEA ZIP."""
    if not zip_bytes:
        raise ParseError("empty ZIP body")
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            return zf.namelist()
    except zipfile.BadZipFile as e:
        raise ParseError(f"could not open ZIP (corrupt or truncated body): {e}") from e


def _resolve_member(zip_bytes: bytes, member_pattern: str) -> str:
    """Find the ZIP member matching member_pattern.

    If `member_pattern` matches a filename exactly inside the ZIP, return it.
    Otherwise treat `member_pattern` as a regex-prefix and look for any
    member starting with the prefix (so YAML can say "wgea_workforce_composition_"
    and we resolve to "wgea_workforce_composition_2025.csv" / "..._2024.csv"
    interchangeably).
    """
    names = list_zip_members(zip_bytes)
    if member_pattern in names:
        return member_pattern
    # Try prefix match (no extension required)
    candidates = [n for n in names if n.startswith(member_pattern)]
    if not candidates:
        # Try regex match
        try:
            rx = re.compile(member_pattern)
            candidates = [n for n in names if rx.search(n)]
        except re.error:
            pass
    if not candidates:
        raise ParseError(
            f"no member in ZIP matches {member_pattern!r}. "
            f"Available members: {names[:8]}"
            + ("..." if len(names) > 8 else "")
        )
    # Pick the newest (highest year suffix) if multiple match.
    candidates.sort(key=_year_score, reverse=True)
    return candidates[0]


def _year_score(name: str) -> int:
    m = re.search(r"_(\d{4})\.csv$", name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def read_csv_from_zip(
    zip_bytes: bytes,
    member_pattern: str,
    *,
    max_rows: int | None = None,
    dtype: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Extract one CSV member from a WGEA ZIP and parse it.

    Args:
        zip_bytes: raw bytes of the ZIP file (as fetched from data.gov.au).
        member_pattern: filename or regex/prefix that matches one CSV inside
            the ZIP. Year-agnostic patterns are recommended (the curated
            YAML's `zip_member` is a prefix).
        max_rows: optional cap on returned rows (useful for tests).
        dtype: optional pandas dtype overrides per column. Most callers pass
            None and let pandas infer.

    Returns:
        DataFrame with the source columns unchanged. Renaming to plain-English
        aliases happens later in `shaping.py`.

    Raises:
        ParseError on corrupt ZIP, missing member, or unreadable CSV.
    """
    member = _resolve_member(zip_bytes, member_pattern)
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            with zf.open(member) as fp:
                df = pd.read_csv(
                    fp,
                    dtype=dtype,
                    nrows=max_rows,
                    low_memory=False,
                )
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError) as e:
        raise ParseError(
            f"could not read {member!r} from ZIP (corrupt or truncated): {e}"
        ) from e
    except pd.errors.ParserError as e:
        raise ParseError(f"pandas could not parse {member!r}: {e}") from e

    df.columns = [_normalize_header(c) for c in df.columns]
    return df


def stream_csv_from_zip(
    zip_bytes: bytes,
    member_pattern: str,
    *,
    max_rows: int,
    row_predicate: Callable[[dict[str, str]], bool] | None = None,
) -> pd.DataFrame:
    """Stream-parse a CSV member from a WGEA ZIP, applying a row predicate.

    Iterates rows with the stdlib `csv.reader` (no pandas) and breaks out
    as soon as `max_rows` rows have been accepted by `row_predicate`. Used
    by the server's fast path for the two largest WGEA CSVs
    (workforce_composition + employee_support) when a caller specifies
    `max_rows` — previously the cold call was paying the full 5-15s
    pandas parse cost regardless of how few rows the caller wanted.

    Args:
        zip_bytes: raw bytes of the ZIP file (as fetched from data.gov.au).
        member_pattern: filename or regex/prefix that matches one CSV inside
            the ZIP (same semantics as `read_csv_from_zip`).
        max_rows: hard cap on accepted rows. Required — the whole point of
            this function is bounded reading. Must be >= 1.
        row_predicate: optional callable(dict[str, str]) -> bool. The dict
            is the row keyed by source column name (values are str). When
            None, all rows match.

    Returns:
        DataFrame whose columns are the source CSV headers (normalised).
        Dtypes default to pandas inference applied to the small accumulated
        list — so an `n_employees` column lands as int64 like the full-parse
        path. The DataFrame has at most `max_rows` rows.

    Raises:
        ParseError on corrupt ZIP, missing member, or unreadable CSV.
        ValueError if `max_rows` < 1.
    """
    if max_rows is None or max_rows < 1:
        raise ValueError(
            f"stream_csv_from_zip requires max_rows >= 1, got {max_rows!r}. "
            "Use read_csv_from_zip for unbounded reads."
        )
    member = _resolve_member(zip_bytes, member_pattern)
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            with zf.open(member) as raw_fp:
                text_fp = io.TextIOWrapper(raw_fp, encoding="utf-8", newline="")
                reader = csv.reader(text_fp)
                try:
                    headers = next(reader)
                except StopIteration as e:
                    raise ParseError(
                        f"CSV member {member!r} is empty (no header row)."
                    ) from e
                headers = [_normalize_header(h) for h in headers]
                n_cols = len(headers)
                collected: list[list[str | None]] = []
                for row in reader:
                    # Tolerate ragged rows: pad with None or truncate to header width.
                    if len(row) < n_cols:
                        row = row + [None] * (n_cols - len(row))  # type: ignore[list-item]
                    elif len(row) > n_cols:
                        row = row[:n_cols]
                    if row_predicate is not None:
                        row_dict = dict(zip(headers, row, strict=False))
                        if not row_predicate(row_dict):
                            continue
                    collected.append(row)
                    if len(collected) >= max_rows:
                        break
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError) as e:
        raise ParseError(
            f"could not read {member!r} from ZIP (corrupt or truncated): {e}"
        ) from e
    except csv.Error as e:
        raise ParseError(f"csv could not parse {member!r}: {e}") from e

    if not collected:
        # Return an empty DataFrame with the same columns so the downstream
        # shaping pipeline still has a well-formed input.
        return pd.DataFrame(columns=headers)
    df = pd.DataFrame(collected, columns=headers)
    # Mirror pandas' default behaviour from read_csv: numeric-looking columns
    # become numeric. apply per-column to_numeric with errors='ignore' so
    # string columns survive intact.
    for col in df.columns:
        coerced = pd.to_numeric(df[col], errors="coerce")
        # Only swap if the entire (non-null) column converted cleanly — that's
        # what pandas' inference would have decided on the original parse.
        non_null_in = df[col].notna() & (df[col] != "")
        non_null_out = coerced.notna()
        if non_null_in.any() and (non_null_in == non_null_out).all():
            df[col] = coerced
    return df


def _normalize_header(c):
    """Strip BOMs + leading/trailing whitespace from CSV column headers."""
    if not isinstance(c, str):
        return c
    return c.lstrip("﻿").strip()


def drop_blank_rows(df: pd.DataFrame, key_columns: list[str]) -> pd.DataFrame:
    """Drop rows where every column in `key_columns` is NaN."""
    present = [c for c in key_columns if c in df.columns]
    if not present:
        return df
    keep_mask = ~df[present].isna().all(axis=1)
    return df.loc[keep_mask].reset_index(drop=True)


# ─── HEADLINE_GAP — Employer Gender Pay Gaps spreadsheet aggregator ─────
#
# WGEA publishes the rolled-up industry "mid-point" gender pay gaps in an
# annual xlsx on wgea.gov.au. The xlsx is per-employer — 8,600+ rows on the
# Employers sheet. We aggregate it server-side into ~20 rows (19 ANZSIC
# divisions + 1 synthetic "All employers" national row) so HEADLINE_GAP
# returns a small, agent-friendly answer instead of 8k+ employer rows.

# Source-column labels on the EGPG xlsx Employers sheet. Pinned here so a
# WGEA header rename surfaces as a ParseError (not a silent column-drop in
# the groupby). Header row is row 3 in the xlsx (header=3 in pandas terms).
_EGPG_SHEET = "2. Employers "  # WGEA ships the trailing space; keep verbatim.
_EGPG_HEADER_ROW = 3
_EGPG_TITLE_ROW = 2  # row 2 col 0 carries "Results based on YYYY-YY ..."

# Headline pay-gap columns. WGEA encodes percentages as fractions (0.214 not
# 21.4) — the aggregator multiplies by 100 so DataResponse callers get the
# user-facing percent. Two snapshots per metric land in the xlsx (current
# year + prior year) — we take only the current-year columns (the first
# occurrence; the prior-year columns share the same label and would clash).
_EGPG_COL_AVG_TOTAL_REM = "Average total remuneration GPG (%)"
_EGPG_COL_AVG_BASE = "Average base salary GPG (%)"
_EGPG_COL_MED_TOTAL_REM = "Median total remuneration GPG (%)"
_EGPG_COL_MED_BASE = "Median base salary GPG (%)"
_EGPG_COL_INDUSTRY = "Industry (ANZSIC Division)"
_EGPG_COL_SECTOR = "Sector"
_EGPG_COL_EMPLOYER = "Employer name"
# Columns used to derive WGEA's published employee-weighted national
# headline figure (~21.1% Mean Total Remuneration GPG for 2024-25).
# Each employer reports an average pay across the whole workforce and a
# % women; combined with the GPG % we can back out average male and
# average female pay per employer, then weight by the employer's
# size-band midpoint to get a workforce-weighted national average.
_EGPG_COL_SIZE = "Employer size range   (# employees)"
_EGPG_COL_AVG_PAY = "Total workforce - average total remuneration ($)*"
_EGPG_COL_PCT_WOMEN = "Total workforce % women"

# WGEA reports employer size as bands. The midpoint estimates here are
# the best we can do without exact headcounts (WGEA holds the raw
# payroll data but doesn't publish per-employer counts in the EGPG
# xlsx). Reproduces WGEA's published 2024-25 Mean Total Remuneration
# GPG of 21.1% within ~0.6pp — see the docstring for derivation.
_EGPG_SIZE_MIDPOINT: dict[str, float] = {
    "<250": 125.0,
    "100-249": 175.0,
    "250-499": 375.0,
    "500-999": 750.0,
    "1000-4999": 2500.0,
    "5000+": 7500.0,
    "5000 or more": 7500.0,
}

_ALL_EMPLOYERS_LABEL = "All employers"

# WGEA publishes the headline numbers on a private-sector basis (matches the
# annual EGPG report's Figure 4). Aggregating across all sectors would mix
# Commonwealth public-sector rows in and shift the mid-point away from
# WGEA's published number. Pin to Private explicitly.
_EGPG_SECTOR_FILTER = "Private"


def _extract_reporting_year(workbook_bytes: bytes) -> str:
    """Pull the "YYYY-YY" reporting-year label out of the xlsx title row.

    Row 2 col 0 of the Employers sheet carries text like
    "Results based on 2024-25 WGEA Gender Equality Reporting". The aggregator
    needs the year label to populate `reporting_year` on every returned row
    so cross-sister consumers can join on it.
    """
    try:
        head = pd.read_excel(
            BytesIO(workbook_bytes),
            sheet_name=_EGPG_SHEET,
            header=None,
            nrows=_EGPG_TITLE_ROW + 1,
            engine="openpyxl",
        )
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        raise ParseError(
            f"could not open EGPG xlsx to read reporting year: {e}"
        ) from e
    try:
        cell = head.iloc[_EGPG_TITLE_ROW, 0]
    except (KeyError, IndexError) as e:
        raise ParseError(
            f"EGPG xlsx title cell (row {_EGPG_TITLE_ROW}) is missing"
        ) from e
    if not isinstance(cell, str):
        raise ParseError(
            f"EGPG xlsx title cell is not text: {cell!r} ({type(cell).__name__})"
        )
    m = re.search(r"(\d{4})-(\d{2,4})", cell)
    if not m:
        raise ParseError(
            f"EGPG xlsx title cell does not contain a YYYY-YY reporting year: {cell!r}"
        )
    return m.group(0)


def parse_egpg_xlsx(workbook_bytes: bytes) -> tuple[pd.DataFrame, str]:
    """Aggregate the EGPG spreadsheet into the HEADLINE_GAP DataFrame.

    Returns:
        (df, reporting_year) where `df` has columns:
          - reporting_year         (WGEA "YYYY-YY" label)
          - anzsic_division        (ANZSIC division name, or "All employers")
          - total_remuneration_gap_pct
          - base_salary_gap_pct
          - median_total_rem_gap_pct
          - median_base_salary_gap_pct
          - employer_count

        Percentages are out of 100 (so 21.4 means 21.4%, not 0.214). Filtered
        to private-sector employers to match WGEA's published headline figures
        (Commonwealth public-sector rows have a different aggregation that
        WGEA reports separately).

    Raises:
        ParseError on missing sheet, missing expected columns, or a corrupt
        xlsx body.
    """
    if not workbook_bytes:
        raise ParseError("empty xlsx body")
    reporting_year = _extract_reporting_year(workbook_bytes)
    try:
        df = pd.read_excel(
            BytesIO(workbook_bytes),
            sheet_name=_EGPG_SHEET,
            header=_EGPG_HEADER_ROW,
            engine="openpyxl",
        )
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        raise ParseError(f"could not parse EGPG xlsx: {e}") from e

    required = [
        _EGPG_COL_INDUSTRY,
        _EGPG_COL_SECTOR,
        _EGPG_COL_EMPLOYER,
        _EGPG_COL_AVG_TOTAL_REM,
        _EGPG_COL_AVG_BASE,
        _EGPG_COL_MED_TOTAL_REM,
        _EGPG_COL_MED_BASE,
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ParseError(
            f"EGPG xlsx is missing expected columns: {missing}. "
            f"Saw: {list(df.columns)[:8]}"
            + ("..." if len(df.columns) > 8 else "")
            + ". WGEA may have changed the spreadsheet shape."
        )

    # Restrict to private sector — WGEA's published "Mid-point of employer
    # GPGs" tables are private-sector only.
    df = df[df[_EGPG_COL_SECTOR].astype("string").str.strip() == _EGPG_SECTOR_FILTER]
    # Drop rows missing the industry tag (a handful of NaN sentinel rows
    # appear at the bottom of recent releases).
    df = df.dropna(subset=[_EGPG_COL_INDUSTRY, _EGPG_COL_EMPLOYER])
    if df.empty:
        raise ParseError(
            f"EGPG xlsx has no {_EGPG_SECTOR_FILTER!r} sector rows after filtering. "
            "WGEA may have changed the sector labels — flag the release shape."
        )

    rows: list[dict[str, Any]] = []
    # Aggregate at THREE levels of size granularity so customers can ask
    # "what's the headline GPG?" and "how does it split by employer size?"
    # from the same dataset:
    #   1. (division × employer_size_band)          — fine grain
    #   2. (division × all_sizes)                   — current default rows
    #   3. (All employers × employer_size_band)     — national size cut
    #   4. (All employers × all_sizes)              — overall national
    # `employer_size_band` of "all" is the sentinel for "any size".
    ALL_SIZES = "all"
    for division, sub in df.groupby(_EGPG_COL_INDUSTRY, sort=True):
        # Per-size-band rows within this division
        if _EGPG_COL_SIZE in sub.columns:
            for size, size_sub in sub.dropna(subset=[_EGPG_COL_SIZE]).groupby(
                _EGPG_COL_SIZE, sort=True
            ):
                if len(size_sub) == 0:
                    continue
                rows.append(_aggregate_industry_row(
                    reporting_year, str(division), size_sub, employer_size=str(size)
                ))
        # All-sizes row for this division
        rows.append(_aggregate_industry_row(
            reporting_year, str(division), sub, employer_size=ALL_SIZES
        ))
    # National per-size-band rows
    if _EGPG_COL_SIZE in df.columns:
        for size, size_sub in df.dropna(subset=[_EGPG_COL_SIZE]).groupby(
            _EGPG_COL_SIZE, sort=True
        ):
            if len(size_sub) == 0:
                continue
            rows.append(_aggregate_industry_row(
                reporting_year, _ALL_EMPLOYERS_LABEL, size_sub, employer_size=str(size)
            ))
    # Synthetic "All employers × all sizes" national row — what answers
    # "what's Australia's gender pay gap?" without any filter.
    rows.append(_aggregate_industry_row(
        reporting_year, _ALL_EMPLOYERS_LABEL, df, employer_size=ALL_SIZES
    ))

    out = pd.DataFrame(rows)
    # Pull "All employers × all_sizes" to the top so HEADLINE_GAP
    # latest() with no filters surfaces the national number first.
    is_all_emp = out["anzsic_division"] == _ALL_EMPLOYERS_LABEL
    is_all_size = out["employer_size_band"] == ALL_SIZES
    is_headline = is_all_emp & is_all_size
    out = pd.concat([out[is_headline], out[~is_headline]], ignore_index=True)
    return out, reporting_year


def _employee_weighted_gpg(
    sub: pd.DataFrame, gpg_col: str
) -> float | None:
    """Compute the WGEA-style employee-weighted mean GPG for a slice.

    WGEA's Scorecard reports a "Mean Total Remuneration GPG" of ~21.1%
    (2024-25) computed from raw aggregated payroll: every employee
    contributes equally, not every employer. The per-employer xlsx does
    not publish exact headcount, but it does publish the employer size
    BAND ('<250', '250-499', '500-999', '1000-4999', '5000+') and the
    employer's average total remuneration + % women.

    Derivation per employer:
      Let A = employer's average total remuneration ($/worker)
          w = % women  (0..1)
          g = employer's GPG  (0..1)
      Solving A = w·f + (1-w)·m and g = (m - f) / m:
          m = A / (1 - w·g)
          f = m · (1 - g)
      where m, f are the employer's average male and female pay.

    Aggregate to national:
      Total male pay   = Σ_employer  n · (1-w) · m
      Total female pay = Σ_employer  n · w · f
      where n is the size-band midpoint.

      national_avg_male   = Σ male_pay / Σ male_count
      national_avg_female = Σ female_pay / Σ female_count
      national GPG = (national_avg_male - national_avg_female) / national_avg_male

    Reproduces WGEA's published 21.1% (2024-25) within ~0.6pp — the
    remaining gap is the size-band midpoint approximation. Returns
    None if the slice lacks enough valid rows to compute.
    """
    needed = [_EGPG_COL_SIZE, _EGPG_COL_AVG_PAY, _EGPG_COL_PCT_WOMEN, gpg_col]
    if any(c not in sub.columns for c in needed):
        return None
    s = sub.dropna(subset=needed).copy()
    if s.empty:
        return None
    s["_n"] = s[_EGPG_COL_SIZE].map(_EGPG_SIZE_MIDPOINT)
    s = s[s["_n"].notna() & (s["_n"] > 0)]
    if s.empty:
        return None
    w = s[_EGPG_COL_PCT_WOMEN].astype(float)
    A = s[_EGPG_COL_AVG_PAY].astype(float)
    g = s[gpg_col].astype(float)
    # Drop employers whose denominators would explode (w·g ≈ 1) — rare,
    # only happens with all-women workforces at extreme gaps.
    denom = 1 - w * g
    keep = denom.abs() > 0.05
    s, w, A, g, denom = s[keep], w[keep], A[keep], g[keep], denom[keep]
    if s.empty:
        return None
    m = A / denom
    f = m * (1 - g)
    male_pay = s["_n"] * (1 - w) * m
    female_pay = s["_n"] * w * f
    male_n = s["_n"] * (1 - w)
    female_n = s["_n"] * w
    total_male_n = male_n.sum()
    total_female_n = female_n.sum()
    if total_male_n <= 0 or total_female_n <= 0:
        return None
    avg_m = male_pay.sum() / total_male_n
    avg_f = female_pay.sum() / total_female_n
    if avg_m <= 0:
        return None
    national_gpg = (avg_m - avg_f) / avg_m
    return round(national_gpg * 100.0, 2)


def _aggregate_industry_row(
    reporting_year: str,
    division: str,
    sub: pd.DataFrame,
    employer_size: str = "all",
) -> dict[str, Any]:
    """One HEADLINE_GAP row for an ANZSIC division (or the All-employers slice).

    Methodology vs WGEA's national headline figures
    ───────────────────────────────────────────────
    HEADLINE_GAP aggregates the per-employer rows from WGEA's Employer Gender
    Pay Gaps Spreadsheet into three flavours of GPG so customers can pick the
    one that matches WGEA's published Scorecard:

      * `total_remuneration_gap_pct` = mean of "Average total remuneration
        GPG (%)" across employers in the slice (unweighted; one employer one vote)
      * `median_total_rem_gap_pct`   = median of per-employer "Median total
        remuneration GPG (%)" — the typical-employer gap
      * `employee_weighted_total_rem_gap_pct` = workforce-weighted using
        each employer's size-band midpoint as a proxy for headcount.
        Reproduces WGEA's published national Mean Total Remuneration GPG
        (~21.1% in 2024-25) within ~0.6pp. THIS is the figure RBA / Treasury
        / financial-media cite as "Australia's gender pay gap".

    Use the employee-weighted measures for headline-figure comparisons; use
    the median measures for "typical employer's gap" analysis; use the mean
    measures when you want unweighted per-employer averages.
    """
    return {
        "reporting_year": reporting_year,
        "anzsic_division": division,
        "employer_size_band": employer_size,
        # mean(per-employer "Average ... GPG (%)") — unweighted average
        # of employer-level mean gaps. One employer one vote.
        "total_remuneration_gap_pct": _pct(sub[_EGPG_COL_AVG_TOTAL_REM].mean()),
        "base_salary_gap_pct": _pct(sub[_EGPG_COL_AVG_BASE].mean()),
        # median(per-employer "Median ... GPG (%)") — "typical employer's
        # gap" view.
        "median_total_rem_gap_pct": _pct(sub[_EGPG_COL_MED_TOTAL_REM].median()),
        "median_base_salary_gap_pct": _pct(sub[_EGPG_COL_MED_BASE].median()),
        # Employee-weighted national-style aggregation — matches WGEA's
        # published Scorecard headline (21.1% Mean Total Remuneration GPG
        # 2024-25). See `_employee_weighted_gpg` for derivation. Computed
        # for All-employers AND every industry slice so customers can do
        # per-industry workforce-weighted comparisons too.
        "employee_weighted_total_rem_gap_pct": _employee_weighted_gpg(
            sub, _EGPG_COL_AVG_TOTAL_REM
        ),
        "employee_weighted_base_salary_gap_pct": _employee_weighted_gpg(
            sub, _EGPG_COL_AVG_BASE
        ),
        "employer_count": int(sub[_EGPG_COL_EMPLOYER].notna().sum()),
    }


def _pct(fraction: float) -> float | None:
    """Convert WGEA's fraction (0.214) to a user-facing percent (21.4).

    WGEA stores GPG values as fractions in the xlsx. NaN propagates as None
    so downstream shaping can drop the cell rather than emit NaN.
    """
    if fraction is None:
        return None
    try:
        f = float(fraction)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return round(f * 100.0, 2)
