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
import re
import zipfile
from collections.abc import Callable
from io import BytesIO

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
