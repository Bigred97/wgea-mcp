"""Live integration tests — hit real data.gov.au.

Run with: pytest -m live

Skipped by default. These tests download the actual ~71MB WGEA Public Data
File ZIP. Pin them under the `live` marker so the regular CI run doesn't
hammer data.gov.au.
"""
from __future__ import annotations

import pytest

from wgea_mcp.cache import Cache
from wgea_mcp.client import WGEAClient
from wgea_mcp.discovery import resolve_latest_zip


@pytest.fixture
async def live_client(tmp_path):
    cache = Cache(db_path=tmp_path / "live-cache.db")
    c = WGEAClient(cache=cache)
    try:
        yield c
    finally:
        await c.aclose()


@pytest.mark.live
async def test_live_ckan_package_show(live_client):
    """data.gov.au CKAN returns the wgea-dataset package."""
    pkg = await live_client.fetch_package("wgea-dataset")
    assert pkg["name"] == "wgea-dataset"
    assert pkg["license_id"] == "cc-by"
    assert isinstance(pkg["resources"], list)
    assert len(pkg["resources"]) > 10


@pytest.mark.live
async def test_live_resolve_latest_zip(live_client):
    """Discovery resolves a current Public Data File from live CKAN."""
    resolved = await resolve_latest_zip(live_client)
    assert resolved.tier == "ckan"
    assert resolved.stale is False
    assert resolved.url.endswith(".zip")
    assert resolved.reporting_year_start >= 2025


@pytest.mark.live
async def test_live_download_zip_header(live_client):
    """Fetching the latest ZIP returns a real ZIP body (first bytes are PK\\x03\\x04)."""
    resolved = await resolve_latest_zip(live_client)
    body = await live_client.fetch_resource(resolved.url, kind="data")
    assert len(body) > 1_000_000  # > 1 MB
    assert body[:2] == b"PK"  # ZIP magic


@pytest.mark.live
async def test_live_zip_contains_workforce_composition(live_client):
    """The live ZIP contains all 8 expected thematic CSVs."""
    from wgea_mcp.parsing import list_zip_members

    resolved = await resolve_latest_zip(live_client)
    body = await live_client.fetch_resource(resolved.url, kind="data")
    members = list_zip_members(body)
    expected = {
        "workforce_composition",
        "workforce_management_statistics",
        "questionnaire_action_on_gender_equality",
        "questionnaire_employee_support",
        "questionnaire_flexible_work",
        "questionnaire_harm_prevention",
        "questionnaire_workplace_overview",
        "questionnaire_catalogue",
    }
    for keyword in expected:
        assert any(keyword in m for m in members), f"missing {keyword!r} in ZIP"


@pytest.mark.live
async def test_live_workforce_composition_parses(live_client):
    """The workforce_composition CSV inside the live ZIP parses cleanly."""
    from wgea_mcp.parsing import read_csv_from_zip

    resolved = await resolve_latest_zip(live_client)
    body = await live_client.fetch_resource(resolved.url, kind="data")
    df = read_csv_from_zip(body, "wgea_workforce_composition_")
    assert len(df) > 100_000  # ~211k expected
    assert "employer_name" in df.columns
    assert "n_employees" in df.columns
    # CBA should be present
    cba = df[df["employer_name"].str.contains("Commonwealth Bank", case=False, na=False)]
    assert len(cba) > 0


@pytest.mark.live
async def test_live_get_data_cba_latest(live_client):
    """End-to-end: server-side get_data for CBA via live discovery + fetch + shape."""
    from wgea_mcp import curated
    from wgea_mcp.parsing import read_csv_from_zip
    from wgea_mcp.shaping import build_response

    resolved = await resolve_latest_zip(live_client)
    body = await live_client.fetch_resource(resolved.url, kind="data")
    cd = curated.get("WORKFORCE_COMPOSITION")
    df = read_csv_from_zip(body, cd.zip_member)

    resp = build_response(
        cd=cd, df=df,
        filters={"employer_name": "CBA"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count > 0
    assert resp.reporting_year is not None
    # The headline buckets exceed 1000 employees
    big_buckets = [r for r in resp.records if r.value and r.value > 1000]
    assert len(big_buckets) > 0


@pytest.mark.live
async def test_live_attribution_string(live_client):
    """Live response carries the CC-BY 3.0 AU attribution."""
    from wgea_mcp import curated
    from wgea_mcp.parsing import read_csv_from_zip
    from wgea_mcp.shaping import build_response

    resolved = await resolve_latest_zip(live_client)
    body = await live_client.fetch_resource(resolved.url, kind="data")
    cd = curated.get("WORKFORCE_COMPOSITION")
    df = read_csv_from_zip(body, cd.zip_member)
    resp = build_response(
        cd=cd, df=df,
        filters={"employer_name": "Atlassian"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert "Workplace Gender Equality Agency" in resp.attribution
    assert "Creative Commons Attribution 3.0 Australia" in resp.attribution


# ─── HEADLINE_GAP live tests (hit wgea.gov.au, not data.gov.au) ─────────


@pytest.mark.live
async def test_live_egpg_xlsx_download(live_client):
    """The Employer Gender Pay Gaps spreadsheet downloads + has xlsx magic."""
    from wgea_mcp import curated

    cd = curated.get("HEADLINE_GAP")
    assert cd is not None and cd.download_url
    body = await live_client.fetch_resource(cd.download_url, kind="data")
    assert len(body) > 100_000  # >100 KB — real spreadsheet is ~2 MB
    # xlsx is a ZIP container under the hood; magic is the same PK header.
    assert body[:2] == b"PK"


@pytest.mark.live
async def test_live_egpg_xlsx_parses_with_current_year(live_client):
    """Live aggregation yields ~20 rows tagged with the latest WGEA year."""
    from wgea_mcp import curated
    from wgea_mcp.parsing import parse_egpg_xlsx

    cd = curated.get("HEADLINE_GAP")
    body = await live_client.fetch_resource(cd.download_url, kind="data")
    df, reporting_year = parse_egpg_xlsx(body)
    # 19 ANZSIC divisions + 1 All employers row = 20 (give some tolerance
    # in case WGEA includes/excludes a division).
    assert 15 <= len(df) <= 22
    assert reporting_year.startswith("20")  # YYYY-YY label
    assert (df["reporting_year"] == reporting_year).all()
    # Every published industry must be present in the All employers row.
    all_emp = df[df["anzsic_division"] == "All employers"]
    assert len(all_emp) == 1


@pytest.mark.live
async def test_live_headline_gap_mining_value_in_realistic_range(live_client):
    """Mining mid-point should land in the 10-30% range every year — wide
    enough not to fail on annual movements, tight enough to catch a unit
    bug (e.g. forgetting the ×100 fraction-to-percent conversion)."""
    from wgea_mcp import curated
    from wgea_mcp.parsing import parse_egpg_xlsx

    cd = curated.get("HEADLINE_GAP")
    body = await live_client.fetch_resource(cd.download_url, kind="data")
    df, _year = parse_egpg_xlsx(body)
    mining = df[df["anzsic_division"] == "Mining"]
    assert len(mining) == 1
    pct = float(mining["total_remuneration_gap_pct"].iloc[0])
    assert 10.0 <= pct <= 30.0, (
        f"Mining mid-point {pct} outside the 10-30% range — unit bug?"
    )


@pytest.mark.live
async def test_live_headline_gap_shape_e2e(live_client):
    """End-to-end: live xlsx → DataResponse for 'what is Australia's pay gap?'"""
    from wgea_mcp import curated
    from wgea_mcp.parsing import parse_egpg_xlsx
    from wgea_mcp.shaping import build_response

    cd = curated.get("HEADLINE_GAP")
    body = await live_client.fetch_resource(cd.download_url, kind="data")
    df, _year = parse_egpg_xlsx(body)
    resp = build_response(
        cd=cd, df=df,
        filters={"anzsic_division": "all"},
        measures="total_remuneration_gap_pct",
        start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    assert resp.row_count == 1
    obs = resp.records[0]
    assert obs.dimensions["anzsic_division"] == "All employers"
    assert obs.unit == "percent"
    # National mid-point of employer GPGs — per WGEA report ~11-13% in
    # recent years; wide tolerance to avoid year-on-year flake.
    assert 5.0 <= obs.value <= 20.0
