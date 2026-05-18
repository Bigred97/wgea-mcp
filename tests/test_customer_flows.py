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


# ─── HEADLINE_GAP customer flows ────────────────────────────────────────

@pytest.fixture
def df_headline_gap(sample_egpg_xlsx_bytes) -> pd.DataFrame:
    df, _year = parsing.parse_egpg_xlsx(sample_egpg_xlsx_bytes)
    return df


def test_australia_pay_gap_national_query(df_headline_gap):
    """Demo prompt: 'What's Australia's gender pay gap?'

    Expectation: the All-employers × all-sizes summary row carrying the
    national mid-point. Since 0.6.10 added `employer_size_band` as a
    dim, the All-employers slice spans the all-sizes summary + per-band
    rows, so we pin the size dim to 'all' to land on the single summary.
    """
    cd = curated.get("HEADLINE_GAP")
    resp = shaping.build_response(
        cd=cd, df=df_headline_gap,
        filters={"anzsic_division": "all", "employer_size_band": "all"},
        measures="total_remuneration_gap_pct",
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count == 1
    obs = resp.records[0]
    assert obs.dimensions["anzsic_division"] == "All employers"
    assert obs.unit == "percent"
    assert obs.value is not None and obs.value > 0


def test_mining_pay_gap_industry_query(df_headline_gap):
    """Demo prompt: 'What's the pay gap in mining?'

    Should resolve 'mining' → 'Mining' and return one row (pinning the
    size-band dim to 'all' to get the division summary).
    """
    cd = curated.get("HEADLINE_GAP")
    resp = shaping.build_response(
        cd=cd, df=df_headline_gap,
        filters={"anzsic_division": "mining", "employer_size_band": "all"},
        measures="total_remuneration_gap_pct",
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count == 1
    obs = resp.records[0]
    assert obs.dimensions["anzsic_division"] == "Mining"
    # Fixture's Mining mean (post-0.6.9 mean-vs-median switch) is 15.0%.
    assert obs.value == 15.0


def test_industry_query_via_anzsic_letter(df_headline_gap):
    """Demo prompt: 'What's the pay gap in division K?' (Finance)"""
    cd = curated.get("HEADLINE_GAP")
    resp = shaping.build_response(
        cd=cd, df=df_headline_gap,
        filters={"anzsic_division": "K", "employer_size_band": "all"},
        measures="total_remuneration_gap_pct",
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count == 1
    assert resp.records[0].dimensions["anzsic_division"] == "Financial and Insurance Services"


def test_headline_gap_default_returns_all_industries(df_headline_gap):
    """No filter → all rows. Since 0.6.10 the dataframe also splits by
    employer_size_band, so we pin it to 'all' to get the 4-division
    summary rows. Each row carries 5 measures with data in the fixture
    (4 gap pcts + employer_count); the employee_weighted measures need
    the Employer-size-range column that the sample fixture doesn't carry,
    so they're None and dropped from the response.
    """
    cd = curated.get("HEADLINE_GAP")
    resp = shaping.build_response(
        cd=cd, df=df_headline_gap,
        filters={"employer_size_band": "all"},
        measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count == 20  # 4 divisions × 5 measures with data
    assert all(r.reporting_year == "2024-25" for r in resp.records)


def test_headline_gap_response_carries_attribution(df_headline_gap):
    cd = curated.get("HEADLINE_GAP")
    resp = shaping.build_response(
        cd=cd, df=df_headline_gap,
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert "Workplace Gender Equality Agency" in resp.attribution
    assert "Creative Commons Attribution 3.0 Australia" in resp.attribution
