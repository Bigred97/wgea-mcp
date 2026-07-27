"""Regression test for the filters-parameter MCP-transport bug.

Every tool that accepts `filters` (get_data, latest, top_n) already has a
lenient `_validate_filters` helper in server.py that json.loads()s a string
argument. But a real MCP client sends tool arguments as JSON, and when the
`filters` parameter was typed `Annotated[dict[str, Any] | None, Field(...)]`,
FastMCP validated the incoming argument against that strict dict-only
Pydantic type BEFORE the function body ran — so a JSON-encoded string
argument (exactly what a well-behaved MCP client sends when it serializes
a dict-shaped parameter to text) was rejected with a raw Pydantic
`dict_type` error, and `_validate_filters` never even got a chance to run.

Calling the decorated tool functions directly with a Python dict (as the
rest of the test suite does, e.g. test_top_n.py, test_bug_regression.py)
never crosses the JSON-RPC boundary where the string arrives, so it cannot
catch this class of bug. This test uses FastMCP's in-process `Client` to
round-trip through the same argument validation a real MCP client triggers.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from wgea_mcp import server
from wgea_mcp.discovery import ResolvedZip
from wgea_mcp.server import mcp

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_ZIP = FIXTURE_DIR / "wgea_sample.zip"


@pytest.fixture
async def mocked_data(monkeypatch, tmp_path):
    """Stub out CKAN discovery and the HTTP fetch so tools run on the bundled
    fixture ZIP, entirely offline. Same pattern as tests/test_top_n.py."""
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
async def test_get_data_accepts_json_string_filters_over_mcp_protocol(mocked_data):
    """A JSON-encoded string `filters` argument, exactly as a real MCP client
    sends it over JSON-RPC, must not be rejected by Pydantic validation
    before _validate_filters ever runs."""
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_data",
            {
                "dataset_id": "WORKFORCE_COMPOSITION",
                "filters": '{"employer_name": "Commonwealth Bank"}',
            },
        )
    data = result.data
    assert data.row_count >= 0
    # Sanity: the filter actually took effect (matches the dict-argument path).
    async with Client(mcp) as client:
        dict_result = await client.call_tool(
            "get_data",
            {
                "dataset_id": "WORKFORCE_COMPOSITION",
                "filters": {"employer_name": "Commonwealth Bank"},
            },
        )
    assert data.row_count == dict_result.data.row_count


@pytest.mark.asyncio
async def test_latest_accepts_json_string_filters_over_mcp_protocol(mocked_data):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "latest",
            {
                "dataset_id": "WORKFORCE_COMPOSITION",
                "filters": '{"employer_name": "Commonwealth Bank"}',
            },
        )
    assert result.data.row_count >= 0


@pytest.mark.asyncio
async def test_top_n_accepts_json_string_filters_over_mcp_protocol(mocked_data):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "top_n",
            {
                "dataset_id": "WORKFORCE_COMPOSITION",
                "measure": "n_employees",
                "n": 5,
                "filters": '{"anzsic_division": "Mining"}',
            },
        )
    assert result.data.row_count >= 0


@pytest.mark.asyncio
async def test_get_data_malformed_json_string_filters_raises_clear_error(mocked_data):
    """Malformed JSON text should still surface _validate_filters' own
    "Try X" hint, not a raw Pydantic dict_type error, over the real
    MCP transport."""
    with pytest.raises(Exception, match="filters must be a JSON object"):
        async with Client(mcp) as client:
            await client.call_tool(
                "get_data",
                {
                    "dataset_id": "WORKFORCE_COMPOSITION",
                    "filters": "{not valid json",
                },
            )
