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
"""
from __future__ import annotations

import re
import zipfile
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
