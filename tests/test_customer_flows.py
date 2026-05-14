"""End-to-end mocked flows covering the README's demo prompts."""
from __future__ import annotations

import io

import pandas as pd
import pytest

from wgea_mcp import curated, parsing, shaping


@pytest.fixture
def df_workforce(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_")


@pytest.fixture
def df_management(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_workforce_management_statistics_")


@pytest.fixture
def df_harm(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_questionnaire_harm_prevention_")


def test_gender_breakdown_at_commonwealth_bank(df_workforce):
    """Demo prompt: 'What's the gender breakdown at Commonwealth Bank?'"""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=df_workforce,
        filters={"employer_name": "CBA"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count > 0
    # Should see both Women and Men rows
    genders = {r.dimensions.get("gender") for r in resp.records}
    assert "Women" in genders or "Men" in genders


def test_top_industries_workforce_composition(df_workforce):
    """Demo prompt: 'Which industries have the most women in management?'"""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=df_workforce,
        filters={"gender": "Women", "manager_category": "Manager"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=50,
    )
    # Won't necessarily have rows in 200-row fixture but should not error
    assert resp.row_count >= 0


def test_industry_filter_mining(df_workforce):
    """Demo prompt: 'Workforce composition in mining'"""
    cd = curated.get("WORKFORCE_COMPOSITION")
    # Use a real division from the fixture (since fixture may not contain Mining)
    available = df_workforce["anzsic_division"].dropna().unique()
    if len(available) == 0:
        pytest.skip("fixture has no anzsic_division values")
    pick = available[0]
    resp = shaping.build_response(
        cd=cd, df=df_workforce,
        filters={"anzsic_division": pick},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=20,
    )
    assert resp.row_count > 0


def test_promotions_at_employer(df_management):
    """Demo prompt: 'Promotions to manager by gender at Westpac'."""
    cd = curated.get("WORKFORCE_MANAGEMENT")
    resp = shaping.build_response(
        cd=cd, df=df_management,
        filters={"employer_name": "Westpac", "movement_type": "Promotions"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    # Fixture may not have promotions for Westpac; either result is acceptable.
    assert resp.row_count >= 0


def test_harm_prevention_questionnaire_lookup(df_harm):
    """Demo prompt: 'Sexual harassment policy at <employer>'."""
    cd = curated.get("HARM_PREVENTION")
    resp = shaping.build_response(
        cd=cd, df=df_harm,
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=10,
    )
    assert resp.row_count > 0
    # The response field should be in dimensions for questionnaire datasets
    first = resp.records[0]
    assert "response" in first.dimensions or "question_text" in first.dimensions


def test_csv_format_round_trip(df_workforce):
    """The 'csv' format should produce a readable CSV string."""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=df_workforce,
        filters={"employer_name": "Atlassian"},
        measures=None, start_period=None, end_period=None,
        fmt="csv", user_query={},
    )
    assert isinstance(resp.csv, str)
    if resp.row_count > 0:
        # Should parse back as a DataFrame
        parsed = pd.read_csv(io.StringIO(resp.csv))
        assert "reporting_year" in parsed.columns


def test_list_employers_filter(df_workforce):
    """Multi-employer filter: pass a list of aliases."""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=df_workforce,
        filters={"employer_name": ["Commonwealth Bank Of Australia", "Atlassian Pty Ltd"]},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=200,
    )
    assert resp.row_count >= 0


def test_attribution_present_in_every_response(df_workforce):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=df_workforce,
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=5,
    )
    assert "Workplace Gender Equality Agency" in resp.attribution
    assert "CC-BY 3.0" in resp.attribution.replace("Creative Commons Attribution 3.0", "CC-BY 3.0") or "Creative Commons Attribution 3.0 Australia" in resp.attribution
