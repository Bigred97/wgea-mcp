"""CSV-in-ZIP parsing."""
from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

from wgea_mcp import parsing
from wgea_mcp.parsing import (
    ParseError,
    drop_blank_rows,
    list_zip_members,
    read_csv_from_zip,
)


def _make_zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_list_zip_members(sample_zip_bytes):
    members = list_zip_members(sample_zip_bytes)
    assert any("workforce_composition" in m for m in members)
    assert any("harm_prevention" in m for m in members)


def test_list_zip_members_empty_body():
    with pytest.raises(ParseError, match="empty ZIP"):
        list_zip_members(b"")


def test_list_zip_members_corrupt_body():
    with pytest.raises(ParseError, match="corrupt"):
        list_zip_members(b"not a zip file at all")


def test_read_csv_exact_name(sample_zip_bytes):
    df = read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_2025.csv")
    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    assert "employer_name" in df.columns
    assert "n_employees" in df.columns


def test_read_csv_prefix_match(sample_zip_bytes):
    """`zip_member: wgea_workforce_composition_` should match the year-suffixed file."""
    df = read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_")
    assert "employer_name" in df.columns


def test_read_csv_max_rows(sample_zip_bytes):
    df = read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_", max_rows=5)
    assert len(df) == 5


def test_read_csv_missing_member(sample_zip_bytes):
    with pytest.raises(ParseError, match="no member in ZIP matches"):
        read_csv_from_zip(sample_zip_bytes, "wgea_does_not_exist")


def test_read_csv_bom_stripped():
    body = _make_zip({"x.csv": "﻿a,b,c\n1,2,3\n"})
    df = read_csv_from_zip(body, "x.csv")
    assert list(df.columns) == ["a", "b", "c"]


def test_drop_blank_rows():
    df = pd.DataFrame({"a": [1, None, 3], "b": ["x", None, "z"]})
    out = drop_blank_rows(df, ["a", "b"])
    assert len(out) == 2
    assert list(out["a"]) == [1, 3]


def test_drop_blank_rows_no_matching_columns():
    df = pd.DataFrame({"a": [1, None]})
    out = drop_blank_rows(df, ["b"])
    # No matching cols → return unchanged
    assert len(out) == 2


def test_read_csv_year_disambiguation():
    """When multiple year-suffixed members match a prefix, pick the newest."""
    body = _make_zip({
        "wgea_x_2024.csv": "a\n1\n",
        "wgea_x_2025.csv": "a\n2\n",
        "wgea_x_2023.csv": "a\n3\n",
    })
    df = read_csv_from_zip(body, "wgea_x_")
    assert df["a"].iloc[0] == 2  # newest year wins


def test_read_csv_regex_fallback():
    body = _make_zip({"foo_bar_baz.csv": "a\n1\n"})
    df = read_csv_from_zip(body, r"foo.*baz")
    assert df["a"].iloc[0] == 1
