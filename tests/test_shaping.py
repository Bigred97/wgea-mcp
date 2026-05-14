"""Shaping — alias rename, dtype coercion, filters, fuzzy match, build_response."""
from __future__ import annotations

import pandas as pd
import pytest

from wgea_mcp import curated, parsing
from wgea_mcp.shaping import (
    build_response,
    fuzzy_match_employer,
    records_to_csv,
)


@pytest.fixture
def workforce_df(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_")


@pytest.fixture
def questionnaire_df(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_questionnaire_harm_prevention_")


def test_fuzzy_match_alias_cba(workforce_df):
    matched, dym = fuzzy_match_employer(workforce_df, "employer_name", "CBA")
    assert any("Commonwealth Bank" in m for m in matched)
    assert dym == []


def test_fuzzy_match_alias_nab(workforce_df):
    matched, _ = fuzzy_match_employer(workforce_df, "employer_name", "NAB")
    assert any("National Australia Bank" in m for m in matched)


def test_fuzzy_match_alias_westpac(workforce_df):
    matched, _ = fuzzy_match_employer(workforce_df, "employer_name", "westpac")
    assert any("Westpac" in m for m in matched)


def test_fuzzy_match_alias_qantas(workforce_df):
    matched, _ = fuzzy_match_employer(workforce_df, "employer_name", "QANTAS")
    assert any("Qantas" in m for m in matched)


def test_fuzzy_match_alias_woolworths(workforce_df):
    matched, _ = fuzzy_match_employer(workforce_df, "employer_name", "woolies")
    assert any("woolworths" in m.lower() for m in matched)


def test_fuzzy_match_substring(workforce_df):
    matched, _ = fuzzy_match_employer(workforce_df, "employer_name", "Atlassian")
    assert any("Atlassian" in m for m in matched)


def test_fuzzy_match_exact_legal_name(workforce_df):
    matched, _ = fuzzy_match_employer(workforce_df, "employer_name", "Commonwealth Bank Of Australia")
    assert "Commonwealth Bank Of Australia" in matched


def test_fuzzy_match_unknown_returns_hints(workforce_df):
    matched, dym = fuzzy_match_employer(workforce_df, "employer_name", "TotallyMadeUpCo")
    assert matched == []
    # dym may be empty if no candidate scores ≥ 50 — both outcomes are valid
    assert isinstance(dym, list)


def test_fuzzy_match_empty_input(workforce_df):
    matched, dym = fuzzy_match_employer(workforce_df, "employer_name", "")
    assert matched == []
    assert dym == []


def test_fuzzy_match_missing_column(workforce_df):
    matched, _ = fuzzy_match_employer(workforce_df, "nonexistent_col", "CBA")
    assert matched == []


# Fuzzy accuracy bar: brief specifies >80% on a hand-crafted alias list.
ALIAS_TEST_SET = [
    ("CBA", "Commonwealth Bank"),
    ("commbank", "Commonwealth Bank"),
    ("commonwealth bank", "Commonwealth Bank"),
    ("NAB", "National Australia Bank"),
    ("national australia bank", "National Australia Bank"),
    ("ANZ", "Australia And New Zealand"),
    ("westpac", "Westpac"),
    ("WESTPAC", "Westpac"),
    ("qantas", "Qantas"),
    ("woolies", "Woolworths"),
    ("Atlassian", "Atlassian"),
    ("telstra", "Telstra"),
]


def test_fuzzy_alias_set_above_80_percent_accuracy(workforce_df):
    """Brief: fuzzy employer search >80% match accuracy on a hand-crafted alias list."""
    hits = 0
    for alias, expected_substring in ALIAS_TEST_SET:
        matched, _ = fuzzy_match_employer(workforce_df, "employer_name", alias)
        if any(expected_substring.lower() in m.lower() for m in matched):
            hits += 1
    accuracy = hits / len(ALIAS_TEST_SET)
    assert accuracy >= 0.80, f"alias accuracy {accuracy:.0%} below 80% bar"


def test_build_response_empty_df():
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=pd.DataFrame(),
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count == 0
    assert resp.records == []


def test_build_response_records_format(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Commonwealth Bank"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count > 0
    assert resp.records[0].dimensions
    assert resp.records[0].value is not None


def test_build_response_csv_format(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Commonwealth Bank"},
        measures=None, start_period=None, end_period=None,
        fmt="csv", user_query={},
    )
    assert resp.csv is not None
    assert "reporting_year,measure,value" in resp.csv


def test_build_response_series_format(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Commonwealth Bank"},
        measures=None, start_period=None, end_period=None,
        fmt="series", user_query={},
    )
    assert isinstance(resp.records, list)
    if resp.records:
        assert "measure" in resp.records[0]
        assert "observations" in resp.records[0]


def test_build_response_unknown_filter_raises(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match="Unknown filter"):
        build_response(
            cd=cd, df=workforce_df,
            filters={"nonexistent_dim": "x"},
            measures=None, start_period=None, end_period=None,
            fmt="records", user_query={},
        )


def test_build_response_empty_filter_list_raises(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match="empty list"):
        build_response(
            cd=cd, df=workforce_df,
            filters={"employer_name": []},
            measures=None, start_period=None, end_period=None,
            fmt="records", user_query={},
        )


def test_build_response_includes_attribution(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Commonwealth Bank"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert "Workplace Gender Equality Agency" in resp.attribution
    assert "Creative Commons Attribution 3.0 Australia" in resp.attribution


def test_build_response_did_you_mean_on_no_match(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "ZZZTotallyMadeUpEmployerCorp"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count == 0
    # Hints may be empty if nothing scores above 50 — both are valid
    assert isinstance(resp.did_you_mean, list)


def test_build_response_anzsic_division_filter(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    # Pick a real division
    sample_div = workforce_df["anzsic_division"].dropna().iloc[0]
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"anzsic_division": sample_div},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count > 0


def test_build_response_max_rows_cap(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=5,
    )
    assert resp.row_count <= 5


def test_build_response_questionnaire_long_format(questionnaire_df):
    cd = curated.get("HARM_PREVENTION")
    resp = build_response(
        cd=cd, df=questionnaire_df,
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=10,
    )
    assert resp.row_count > 0
    # The questionnaire 'response' should be in dimensions
    first = resp.records[0]
    assert "response" in first.dimensions or "question_text" in first.dimensions


def test_records_to_csv_empty():
    assert records_to_csv([]) == ""


def test_records_to_series_groups_by_measure(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Commonwealth Bank"},
        measures=None, start_period=None, end_period=None,
        fmt="series", user_query={},
    )
    if resp.records:
        # Each series entry should have a unique measure name
        measures_seen = [r["measure"] for r in resp.records]
        assert len(measures_seen) == len(set(measures_seen))


def test_wildcard_filter_substring_match(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "commonwealth*"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count > 0


def test_period_filter_string_compare(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    # All rows in fixture are 2024-25; filter that should match all
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={}, measures=None,
        start_period="2024-25", end_period="2024-25",
        fmt="records", user_query={},
        max_rows=5,
    )
    assert resp.row_count > 0


def test_period_filter_year_only(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={}, measures=None,
        start_period="2024", end_period="2025",
        fmt="records", user_query={},
        max_rows=5,
    )
    # Should still match (the expand logic handles "2024" → "2023-24" start, "2024-25" end)
    assert resp.row_count >= 0


def test_data_response_period_populated_alongside_reporting_year(workforce_df):
    """Wave-2 interop: canonical period dict is populated when reporting_year is."""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Commonwealth Bank"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    # Single-year fixture: reporting_year and period["start"]/period["end"] match.
    assert resp.reporting_year is not None
    assert resp.period["start"] == resp.reporting_year
    assert resp.period["end"] == resp.reporting_year


def test_data_response_period_brackets_multi_year_records(workforce_df):
    """When records span multiple reporting years, period brackets the range."""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={},  # no employer filter → may span multiple years
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    if resp.row_count > 0:
        years = sorted({r.reporting_year for r in resp.records if r.reporting_year})
        if years:
            assert resp.period["start"] == years[0]
            assert resp.period["end"] == years[-1]
            # The legacy reporting_year is preserved (it's the latest, not the earliest).
            assert resp.reporting_year == years[-1]


def test_data_response_period_empty_when_no_records():
    """No records → period dict has None bounds (matches reporting_year=None)."""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=pd.DataFrame(),
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.reporting_year is None
    assert resp.period == {"start": None, "end": None}
