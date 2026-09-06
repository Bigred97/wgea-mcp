"""Tests for the top_n convenience tool.

top_n ranks rows by a measure and returns the top (or bottom) N. It's the
most common agent workflow — "show me the top 10 X by Y" — collapsed into
a single server-side call. For wgea-mcp it ranks across one WGEA reporting
year (the latest by default, or a specific year via `reporting_year=`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wgea_mcp import server
from wgea_mcp.discovery import ResolvedZip

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_ZIP = FIXTURE_DIR / "wgea_sample.zip"


@pytest.fixture
async def mocked_data(monkeypatch, tmp_path):
    """Stub out CKAN discovery and the HTTP fetch so top_n runs on the bundled
    fixture ZIP. Mirrors the regression-test pattern: replace resolve_latest_zip
    and WGEAClient.fetch_resource at the module-attribute level."""
    sample_bytes = SAMPLE_ZIP.read_bytes()

    async def _fake_resolve(client):
        return ResolvedZip(
            url="https://example.invalid/wgea_test.zip",
            reporting_year_label="2024-25",
            reporting_year_start=2024,
            tier="seed",
            stale=False,
            reason=None,
        )

    async def _fake_fetch(self, url, *, kind="data"):
        return sample_bytes

    from wgea_mcp.client import WGEAClient

    monkeypatch.setattr(server, "resolve_latest_zip", _fake_resolve)
    monkeypatch.setattr(WGEAClient, "fetch_resource", _fake_fetch)
    server.reset_df_cache_for_tests()
    yield
    server.reset_df_cache_for_tests()
    await server.reset_client_for_tests()


@pytest.mark.asyncio
async def test_top_n_returns_top_rows_by_measure(mocked_data):
    """Top-5 ranking by n_employees on WORKFORCE_COMPOSITION."""
    r = await server.top_n("WORKFORCE_COMPOSITION", "n_employees", n=5)
    assert 1 <= r.row_count <= 5
    values = [rec.value for rec in r.records]
    assert values == sorted(values, reverse=True)


@pytest.mark.asyncio
async def test_top_n_bottom_direction_reverses(mocked_data):
    """direction='bottom' returns ascending (smallest-first) values."""
    r = await server.top_n(
        "WORKFORCE_COMPOSITION", "n_employees", n=5, direction="bottom"
    )
    assert 1 <= r.row_count <= 5
    values = [rec.value for rec in r.records]
    assert values == sorted(values)


@pytest.mark.asyncio
async def test_top_n_applies_filters_before_ranking(mocked_data):
    """Filters from the `filters=` arg are applied before the sort/slice."""
    r = await server.top_n(
        "WORKFORCE_COMPOSITION", "n_employees", n=3,
        filters={"gender": "Women"},
    )
    # Every returned row must carry the gender=Women filter.
    for rec in r.records:
        assert rec.dimensions.get("gender") == "Women"


@pytest.mark.asyncio
async def test_top_n_caps_at_available_rows(mocked_data):
    """If the slice asks for more than what's available, return what we have."""
    r = await server.top_n(
        "WORKFORCE_COMPOSITION", "n_employees", n=100,
        filters={"anzsic_division": "Mining"},
    )
    assert r.row_count <= 100


@pytest.mark.asyncio
async def test_top_n_reporting_year_filter(mocked_data):
    """Explicit reporting_year restricts the ranking to that year only."""
    r = await server.top_n(
        "WORKFORCE_COMPOSITION", "n_employees", n=5,
        reporting_year="2024-25",
    )
    # The fixture is the 2024-25 release so all rows carry that year.
    assert r.row_count > 0
    for rec in r.records:
        assert rec.reporting_year == "2024-25"


@pytest.mark.asyncio
async def test_top_n_reporting_year_bare_year_filter(mocked_data):
    """A bare 'YYYY' reporting_year restricts to the single reporting year
    starting in that year ('2024' -> '2024-25'), not a widened range.

    Regression test for the _expand_period_input start/end asymmetry fix:
    the fixture is the 2024-25 release, so reporting_year='2024' must return
    exactly those rows (and only that one reporting year), never zero rows
    and never a range spanning multiple reporting years.
    """
    r = await server.top_n(
        "WORKFORCE_COMPOSITION", "n_employees", n=5,
        reporting_year="2024",
    )
    assert r.row_count > 0
    years = {rec.reporting_year for rec in r.records}
    assert years == {"2024-25"}


@pytest.mark.asyncio
async def test_top_n_envelope_preserved(mocked_data):
    """Trust-contract fields survive the rank-and-slice transformation."""
    r = await server.top_n("WORKFORCE_COMPOSITION", "n_employees", n=3)
    assert r.source == "Workplace Gender Equality Agency (WGEA)"
    assert "Creative Commons" in r.attribution
    assert r.source_url


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_n_rejects_empty_measure():
    with pytest.raises(ValueError, match="measure is required"):
        await server.top_n("WORKFORCE_COMPOSITION", "", n=5)


@pytest.mark.asyncio
async def test_top_n_rejects_non_string_measure():
    with pytest.raises(ValueError, match="measure is required"):
        await server.top_n("WORKFORCE_COMPOSITION", 123, n=5)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_top_n_rejects_bad_direction():
    with pytest.raises(ValueError, match="direction must be"):
        await server.top_n(
            "WORKFORCE_COMPOSITION", "n_employees", n=5,
            direction="sideways",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_top_n_rejects_n_zero():
    with pytest.raises(ValueError, match=">= 1"):
        await server.top_n("WORKFORCE_COMPOSITION", "n_employees", n=0)


@pytest.mark.asyncio
async def test_top_n_rejects_n_too_large():
    with pytest.raises(ValueError, match="<= 100"):
        await server.top_n("WORKFORCE_COMPOSITION", "n_employees", n=500)


@pytest.mark.asyncio
async def test_top_n_rejects_n_bool():
    with pytest.raises(ValueError, match="positive integer"):
        await server.top_n(
            "WORKFORCE_COMPOSITION", "n_employees", n=True  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_top_n_rejects_unknown_measure(mocked_data):
    """Unknown measure → ValueError listing valid measure keys + fuzzy hint."""
    with pytest.raises(ValueError, match="Unknown measure"):
        await server.top_n(
            "WORKFORCE_COMPOSITION", "not_a_real_measure", n=5
        )


@pytest.mark.asyncio
async def test_top_n_rejects_uncurated_dataset():
    """Non-curated dataset_id → ValueError with the unknown-dataset hint."""
    with pytest.raises(ValueError, match="not a curated"):
        await server.top_n("RANDOM_RAW_ID", "n_employees", n=5)


# ---------------------------------------------------------------------------
# 0.6.19: rank full population before the row ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_n_ranks_rows_past_where_a_prefix_cap_would_cut(
    mocked_data, monkeypatch
):
    """True leaders living past a file-order prefix must still win.

    Simulates the old bug: a HARD_MAX-sized prefix in file order would only
    see small early values; the real maxima sit at the end of the frame.
    for_ranking + full-parse must surface those maxima.
    """
    import pandas as pd
    from wgea_mcp.parsing import read_csv_from_zip

    sample = read_csv_from_zip(SAMPLE_ZIP.read_bytes(), "wgea_workforce_composition_")
    template = sample.iloc[0:1].copy()
    # Tiny synthetic ceiling so we do not need 10k fixture rows.
    fake_ceiling = 8
    monkeypatch.setattr(server, "_HARD_MAX_ROWS", fake_ceiling)

    rows = []
    for i in range(fake_ceiling + 5):
        row = template.iloc[0].copy()
        row["employer_name"] = f"Employer_{i:03d}"
        row["employer_abn"] = f"{10000000000 + i}"
        # Early (prefix) rows are tiny; the true leaders are past the ceiling.
        row["n_employees"] = 1 if i < fake_ceiling else 10_000 + i
        rows.append(row)
    big = pd.DataFrame(rows)

    async def _fake_full(cd):
        return big, "https://example.invalid/wgea_test.zip", "2024-25", False, None

    async def _streaming_must_not_run(*a, **kw):
        raise AssertionError(
            "top_n for_ranking must use full-parse, not the streaming prefix cap"
        )

    monkeypatch.setattr(server, "_fetch_and_parse", _fake_full)
    monkeypatch.setattr(server, "_fetch_and_parse_streaming", _streaming_must_not_run)
    server.reset_df_cache_for_tests()

    r = await server.top_n("WORKFORCE_COMPOSITION", "n_employees", n=3)
    values = [rec.value for rec in r.records]
    assert values == sorted(values, reverse=True)
    assert values[0] >= 10_000 + fake_ceiling
    assert all(v >= 10_000 for v in values), (
        f"expected only post-ceiling leaders, got {values}"
    )
    assert r.truncated_at is None
    if r.caveat:
        assert "first" not in r.caveat and "not the full population" not in r.caveat


def test_build_response_ranks_before_max_rows_file_order_cap():
    """Direct shaping unit: rank_by beats a file-order max_rows prefix."""
    import pandas as pd
    from wgea_mcp import curated
    from wgea_mcp.parsing import read_csv_from_zip
    from wgea_mcp.shaping import build_response

    sample = read_csv_from_zip(SAMPLE_ZIP.read_bytes(), "wgea_workforce_composition_")
    template = sample.iloc[0:1].copy()
    rows = []
    for i in range(12):
        row = template.iloc[0].copy()
        row["employer_name"] = f"RankCo_{i:02d}"
        row["employer_abn"] = f"{20000000000 + i}"
        row["n_employees"] = i + 1  # file order ascending; true top is at the end
        rows.append(row)
    df = pd.DataFrame(rows)
    cd = curated.get("WORKFORCE_COMPOSITION")

    # File-order cap alone would return the smallest values.
    capped = build_response(
        cd=cd, df=df, filters={}, measures="n_employees",
        start_period=None, end_period=None, fmt="records", user_query={},
        max_rows=3,
    )
    assert [rec.value for rec in capped.records] == [1, 2, 3]
    assert capped.truncated_at == 12

    # Rank-before-cap returns the true top-3 and does not set truncated_at.
    ranked = build_response(
        cd=cd, df=df, filters={}, measures="n_employees",
        start_period=None, end_period=None, fmt="records", user_query={},
        max_rows=3,  # ignored when rank_* is set
        rank_by="n_employees", rank_n=3, rank_descending=True,
    )
    assert [rec.value for rec in ranked.records] == [12, 11, 10]
    assert ranked.truncated_at is None
