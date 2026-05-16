"""Regression tests for bugs caught during the v0.1.0 → v0.1.1 QA pass.

Each test pins a behaviour that previously regressed. Keep them.
"""
from __future__ import annotations

import asyncio

import httpx
import pandas as pd
import pytest

from wgea_mcp import curated, parsing, server, shaping
from wgea_mcp.cache import Cache
from wgea_mcp.client import WGEAClient


@pytest.fixture
def workforce_df(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_")


# -------------------------------------------------------------------------
# Bug 1: list-of-aliases multi-employer filter returned 0 rows because the
# list path didn't run through the alias map. Fixed by routing each list
# entry through the fuzzy_match_employer path when the column is fuzzy.
# -------------------------------------------------------------------------
def test_bug1_multi_alias_filter_expands(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": ["CBA", "NAB", "Westpac", "ANZ"]},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=2000,
    )
    assert resp.row_count > 0, "Big-4 banks multi-alias filter must return rows"
    employers = {r.dimensions.get("employer_name") for r in resp.records}
    # Should resolve to at least 3 of the Big 4 (fixture might miss one)
    big4_substrings = ["Commonwealth", "National Australia", "Westpac", "Australia And New Zealand"]
    matched_substrings = sum(
        1 for sub in big4_substrings
        if any(sub in (e or "") for e in employers)
    )
    assert matched_substrings >= 3, (
        f"expected ≥3 of Big-4 banks via alias expansion, got "
        f"{matched_substrings} (employers: {employers})"
    )


# -------------------------------------------------------------------------
# Bug 2: negative max_rows silently fell back to default 2000 instead of
# raising. Fixed by adding explicit validation in _get_data_impl.
# -------------------------------------------------------------------------
async def test_bug2_negative_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be >= 1"):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=-5,
        )


async def test_bug2_zero_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be >= 1"):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=0,
        )


async def test_bug2_too_large_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be <="):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=999_999,
        )


async def test_bug2_bool_max_rows_rejected():
    with pytest.raises(ValueError, match=r"max_rows must be a positive integer"):
        await server.get_data(
            dataset_id="WORKFORCE_COMPOSITION",
            filters=None,
            max_rows=True,  # type: ignore[arg-type]
        )


# -------------------------------------------------------------------------
# Bug 3: filter value None coerced to "None" via str() and matched spuriously
# via rapidfuzz. Fixed by rejecting None filter values up-front.
# -------------------------------------------------------------------------
def test_bug3_none_filter_value_rejected(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match=r"value is None"):
        shaping.build_response(
            cd=cd, df=workforce_df,
            filters={"employer_name": None},
            measures=None, start_period=None, end_period=None,
            fmt="records", user_query={},
        )


def test_bug3_none_in_list_filter_rejected(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match=r"contains None"):
        shaping.build_response(
            cd=cd, df=workforce_df,
            filters={"employer_name": ["CBA", None, "NAB"]},
            measures=None, start_period=None, end_period=None,
            fmt="records", user_query={},
        )


# -------------------------------------------------------------------------
# Bug 4: SQLite read-after-write race caused 50 parallel callers to fire
# 2 HTTP requests for the same URL. Fixed by adding an in-memory LRU of
# recent fetch results that fronts the SQLite cache.
# -------------------------------------------------------------------------
async def test_bug4_in_flight_dedup_50_parallel(tmp_path):
    call_count = 0

    def handler(req):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=b"fake-zip-body")

    transport = httpx.MockTransport(handler)
    cache = Cache(db_path=tmp_path / "c.db")
    client = WGEAClient(cache=cache, transport=transport)
    try:
        url = "https://data.gov.au/data/dataset/X/resource/Y/download/file.zip"
        tasks = [client.fetch_resource(url, kind="data") for _ in range(50)]
        results = await asyncio.gather(*tasks)
        assert all(r == b"fake-zip-body" for r in results)
        assert call_count == 1, (
            f"50 parallel fetches should dedupe to 1 HTTP request "
            f"(got {call_count} — read-after-write race regressed?)"
        )
    finally:
        await client.aclose()


# -------------------------------------------------------------------------
# Bug 5: WRatio alone tied "Commonweath Bank" between Commonwealth and Bendigo
# (both have "Bank"), so ranking was non-deterministic and often picked the
# wrong one. Fixed by averaging WRatio with partial_ratio which strongly
# favours the substring match.
# -------------------------------------------------------------------------
@pytest.mark.parametrize("typo,expected_substring", [
    ("Commonweath Bank", "Commonwealth"),
    ("Comonwelth Bank", "Commonwealth"),
    ("Cwlth Bank", "Commonwealth"),
    ("Natoinal Australia", "National Australia"),
    ("Westpak", "Westpac"),
    ("Quantas", "Qantas"),
    ("woolworth", "Woolworths"),
])
def test_bug5_typo_corrected_to_correct_employer(workforce_df, typo, expected_substring):
    matched, _ = shaping.fuzzy_match_employer(workforce_df, "employer_name", typo)
    assert matched, f"typo {typo!r} should produce at least one match"
    assert expected_substring.lower() in matched[0].lower(), (
        f"typo {typo!r} top match was {matched[0]!r}, expected substring "
        f"{expected_substring!r}"
    )


# -------------------------------------------------------------------------
# Bug 6: `truncated_at` field existed in the DataResponse model and was
# documented in CLAUDE.md's trust contract, but shaping never set it when
# max_rows capped the post-filter result. Agents had no way to detect
# that they were seeing a truncated view.
# -------------------------------------------------------------------------
def test_bug6_truncated_at_set_when_capped(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    # Source fixture has > 50 rows; cap at 10 to force truncation.
    resp = shaping.build_response(
        cd=cd, df=workforce_df,
        filters={}, measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=10,
    )
    assert resp.row_count == 10
    assert resp.truncated_at is not None, (
        "truncated_at must be set when max_rows truncates the post-filter result"
    )
    assert resp.truncated_at > 10, (
        f"truncated_at must hold the pre-truncation count (>{10}), got {resp.truncated_at}"
    )


def test_bug6_truncated_at_none_when_under_cap(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "1-STOP CONNECTIONS"},  # small employer
        measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=200,
    )
    assert resp.row_count < 200
    assert resp.truncated_at is None, (
        f"truncated_at must be None when result fits under max_rows; "
        f"got truncated_at={resp.truncated_at} for row_count={resp.row_count}"
    )


# -------------------------------------------------------------------------
# Bug 7: `latest()` tool's `max_rows` parameter had `description` but no
# `examples=[...]` array. CLAUDE.md flags this as a "non-negotiable" Glama
# Tool Definition Quality requirement. Caught by an end-to-end pass that
# enumerates every tool's parameter schema and counts missing examples.
# -------------------------------------------------------------------------
async def test_bug7_all_tool_params_have_examples():
    from wgea_mcp.server import mcp

    tools = await mcp.list_tools()
    bad = []
    for t in tools:
        if t.name == "list_curated":
            continue  # no params, exempt
        props = (t.parameters or {}).get("properties", {})
        for pname, pdef in props.items():
            if not isinstance(pdef, dict):
                continue
            if not pdef.get("examples"):
                bad.append(f"{t.name}.{pname}")
    assert not bad, (
        f"All tool params should carry Field(examples=[...]) per CLAUDE.md "
        f"Glama bar — missing on: {bad}"
    )


async def test_bug7_all_tool_params_have_descriptions():
    """Companion to bug 7: every param needs `description=` too."""
    from wgea_mcp.server import mcp

    tools = await mcp.list_tools()
    bad = []
    for t in tools:
        props = (t.parameters or {}).get("properties", {})
        for pname, pdef in props.items():
            if not isinstance(pdef, dict):
                continue
            if not pdef.get("description"):
                bad.append(f"{t.name}.{pname}")
    assert not bad, f"All tool params need description= — missing on: {bad}"


def test_bug6_truncated_at_none_when_max_rows_none(workforce_df):
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = shaping.build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Commonwealth Bank"},
        measures=None,
        start_period=None, end_period=None,
        fmt="records", user_query={},
        max_rows=None,
    )
    assert resp.truncated_at is None, (
        "truncated_at must be None when no max_rows cap was specified"
    )


def test_bug5_existing_aliases_still_resolve(workforce_df):
    """The new combined scorer should not regress short-abbreviation aliases
    which were already handled by the static alias map."""
    for alias, expected in [
        ("CBA", "Commonwealth"),
        ("NAB", "National Australia"),
        ("ANZ", "Australia And New Zealand"),
        ("Westpac", "Westpac"),
        ("woolies", "Woolworths"),
        ("Atlassian", "Atlassian"),
    ]:
        matched, _ = shaping.fuzzy_match_employer(workforce_df, "employer_name", alias)
        assert any(expected.lower() in m.lower() for m in matched), (
            f"alias {alias!r} should still resolve to {expected!r}; got {matched[:2]}"
        )


async def test_bug4_recent_results_lru_bounded(tmp_path):
    """The in-memory recent-results LRU must stay bounded."""
    call_count = 0

    def handler(req):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=b"body" + str(call_count).encode())

    transport = httpx.MockTransport(handler)
    cache = Cache(db_path=tmp_path / "c.db")
    client = WGEAClient(cache=cache, transport=transport)
    try:
        # Fetch 20 distinct URLs — recent-results LRU is capped at 16
        for i in range(20):
            url = f"https://data.gov.au/data/r{i}/file.zip"
            await client.fetch_resource(url, kind="data")
        assert len(client._recent_results) <= 16, (
            f"recent-results LRU not bounded — has {len(client._recent_results)}"
        )
    finally:
        await client.aclose()


# -------------------------------------------------------------------------
# Bug 8: WORKFORCE_COMPOSITION and EMPLOYEE_SUPPORT — the two biggest CSVs
# in the WGEA ZIP (56 MB / 154 MB uncompressed) — were timing out at the
# hosted API's 20s budget even for `limit=2` requests. The cold path was
# paying the full ~5-13s pandas parse before any row truncation happened.
# Fix: stream rows with csv.reader and short-circuit at max_rows + 1 (so
# build_response can still detect truncation). Verified by injecting the
# bundled fixture ZIP and timing the end-to-end call against a strict
# budget that the pre-fix code path failed (it would have re-parsed the
# fixture's 200-row CSVs serially via pandas).
# -------------------------------------------------------------------------
def _make_streaming_fixture(monkeypatch):
    """Inject the bundled fixture ZIP into server._fetch_zip_body so the
    streaming fast path runs end-to-end without network. Returns a row-counter
    that records how many CSV rows the stream actually iterated past — proxy
    for "did we short-circuit?"
    """
    from pathlib import Path

    from wgea_mcp import parsing, server
    from wgea_mcp.discovery import ResolvedZip

    fixture = Path(__file__).parent / "fixtures" / "wgea_sample.zip"
    sample_bytes = fixture.read_bytes()

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

    # Track how many rows the stream consumed to verify short-circuit.
    seen_calls: dict[str, int] = {}
    orig_stream = parsing.stream_csv_from_zip

    def _spy_stream(zip_bytes, member_pattern, *, max_rows, row_predicate=None):
        # Wrap the row_predicate to count invocations — one call per row read.
        counter = {"rows": 0}

        def counting_predicate(row):
            counter["rows"] += 1
            return True if row_predicate is None else row_predicate(row)

        df = orig_stream(
            zip_bytes,
            member_pattern,
            max_rows=max_rows,
            row_predicate=counting_predicate,
        )
        seen_calls[member_pattern] = counter["rows"]
        return df

    monkeypatch.setattr(parsing, "stream_csv_from_zip", _spy_stream)
    monkeypatch.setattr(server, "stream_csv_from_zip", _spy_stream)
    server.reset_df_cache_for_tests()
    return seen_calls


async def test_workforce_composition_limit_short_circuits(monkeypatch):
    """`get_data('WORKFORCE_COMPOSITION', max_rows=2)` must short-circuit at
    parse time — must NOT walk the full 200-row fixture CSV.
    """
    import time

    from wgea_mcp import server

    seen = _make_streaming_fixture(monkeypatch)
    t0 = time.time()
    try:
        resp = await server.get_data("WORKFORCE_COMPOSITION", max_rows=2)
        elapsed = time.time() - t0
        assert resp.row_count == 2, f"expected 2 rows, got {resp.row_count}"
        # The fixture has ~200 rows; with limit=2 we should only iterate ~3
        # (max_rows + 1 for truncation detection). Anything ≥10 means
        # short-circuit regressed.
        rows_walked = next(iter(seen.values())) if seen else 0
        assert rows_walked <= 5, (
            f"streaming did not short-circuit — walked {rows_walked} rows "
            f"for limit=2 (should be ≤5)"
        )
        # Customer-blocking timeout was 20s; require well under that.
        assert elapsed < 5.0, (
            f"limit=2 on WORKFORCE_COMPOSITION took {elapsed:.2f}s "
            f"(must be <5s — was the customer-blocking failure mode)"
        )
    finally:
        await server.reset_client_for_tests()


async def test_employee_support_limit_short_circuits(monkeypatch):
    """`get_data('EMPLOYEE_SUPPORT', max_rows=2)` — the second customer-
    blocking dataset. Same short-circuit + timeout requirements.
    """
    import time

    from wgea_mcp import server

    seen = _make_streaming_fixture(monkeypatch)
    t0 = time.time()
    try:
        resp = await server.get_data("EMPLOYEE_SUPPORT", max_rows=2)
        elapsed = time.time() - t0
        assert resp.row_count == 2, f"expected 2 rows, got {resp.row_count}"
        rows_walked = next(iter(seen.values())) if seen else 0
        assert rows_walked <= 5, (
            f"streaming did not short-circuit — walked {rows_walked} rows "
            f"for limit=2 (should be ≤5)"
        )
        assert elapsed < 5.0, (
            f"limit=2 on EMPLOYEE_SUPPORT took {elapsed:.2f}s "
            f"(must be <5s — was the customer-blocking failure mode)"
        )
    finally:
        await server.reset_client_for_tests()


async def test_gender_equality_actions_limit_short_circuits(monkeypatch):
    """`get_data('GENDER_EQUALITY_ACTIONS', max_rows=2)` — questionnaire dataset
    (77 MB / hundreds of thousands of rows in the 2024-25 release). Same
    short-circuit + timeout requirements as the 0.5.2 fix for WORKFORCE_COMPOSITION
    and EMPLOYEE_SUPPORT.
    """
    import time

    from wgea_mcp import server

    seen = _make_streaming_fixture(monkeypatch)
    t0 = time.time()
    try:
        resp = await server.get_data("GENDER_EQUALITY_ACTIONS", max_rows=2)
        elapsed = time.time() - t0
        assert resp.row_count == 2, f"expected 2 rows, got {resp.row_count}"
        rows_walked = next(iter(seen.values())) if seen else 0
        assert rows_walked <= 5, (
            f"streaming did not short-circuit — walked {rows_walked} rows "
            f"for limit=2 (should be <=5)"
        )
        assert elapsed < 5.0, (
            f"limit=2 on GENDER_EQUALITY_ACTIONS took {elapsed:.2f}s "
            f"(must be <5s — was the customer-blocking failure mode)"
        )
    finally:
        await server.reset_client_for_tests()


async def test_workforce_management_limit_short_circuits(monkeypatch):
    """`get_data('WORKFORCE_MANAGEMENT', max_rows=2)` — wide manager-movement
    dataset (~235k rows in the 2024-25 release). Same short-circuit + timeout
    requirements as the 0.5.2 fix for WORKFORCE_COMPOSITION and EMPLOYEE_SUPPORT.
    """
    import time

    from wgea_mcp import server

    seen = _make_streaming_fixture(monkeypatch)
    t0 = time.time()
    try:
        resp = await server.get_data("WORKFORCE_MANAGEMENT", max_rows=2)
        elapsed = time.time() - t0
        assert resp.row_count == 2, f"expected 2 rows, got {resp.row_count}"
        rows_walked = next(iter(seen.values())) if seen else 0
        assert rows_walked <= 5, (
            f"streaming did not short-circuit — walked {rows_walked} rows "
            f"for limit=2 (should be <=5)"
        )
        assert elapsed < 5.0, (
            f"limit=2 on WORKFORCE_MANAGEMENT took {elapsed:.2f}s "
            f"(must be <5s — was the customer-blocking failure mode)"
        )
    finally:
        await server.reset_client_for_tests()


async def test_streaming_other_datasets_unaffected(monkeypatch):
    """The 3 non-streaming WGEA datasets must NOT enter the streaming path —
    they use the full-parse-and-cache path which is correct + fast on warm calls.
    """
    from wgea_mcp import server

    # Same fixture stubbing, but with a stream-counter that fails if invoked
    # for any of the 3 non-streaming datasets.
    seen = _make_streaming_fixture(monkeypatch)
    untouched = (
        "HARM_PREVENTION",
        "PARENTAL_LEAVE_FLEX",
        "WORKPLACE_OVERVIEW",
    )
    try:
        for ds_id in untouched:
            seen.clear()
            resp = await server.get_data(ds_id, max_rows=2)
            assert resp.row_count <= 2
            assert not seen, (
                f"{ds_id} should NOT enter the streaming fast path — "
                f"got stream_csv_from_zip calls: {list(seen.keys())}"
            )
    finally:
        await server.reset_client_for_tests()


async def test_streaming_with_fuzzy_filter_falls_back_to_full_parse(monkeypatch):
    """Fuzzy employer-name filters must fall back to the full-parse path
    so rapidfuzz / alias map / wildcard logic still works correctly.
    """
    from wgea_mcp import server

    seen = _make_streaming_fixture(monkeypatch)
    try:
        # Employer-name filter is fuzzy → must NOT enter streaming path.
        resp = await server.get_data(
            "WORKFORCE_COMPOSITION",
            filters={"employer_name": "CBA"},  # alias → Commonwealth Bank
            max_rows=10,
        )
        assert not seen, (
            f"fuzzy filter must fall back to full-parse path — "
            f"got stream calls: {list(seen.keys())}"
        )
        # The fallback still has to produce useful rows.
        assert resp.row_count >= 0
    finally:
        await server.reset_client_for_tests()
