"""Pydantic model shape + defaults."""
from __future__ import annotations

from datetime import datetime, timezone

from wgea_mcp.models import (
    ColumnDetail,
    DataResponse,
    DatasetDetail,
    DatasetSummary,
    Observation,
)


def test_dataset_summary_minimal():
    s = DatasetSummary(id="X", name="Name")
    assert s.id == "X"
    assert s.is_curated is False
    assert s.update_frequency is None


def test_observation_defaults():
    o = Observation()
    assert o.value is None
    assert o.reporting_year is None
    assert o.measure is None
    assert o.dimensions == {}


def test_data_response_attribution_string():
    r = DataResponse(
        dataset_id="X", dataset_name="X",
        retrieved_at=datetime.now(timezone.utc),
        source_url="https://data.gov.au/",
    )
    assert "Workplace Gender Equality Agency" in r.attribution
    assert "Creative Commons Attribution 3.0 Australia" in r.attribution
    assert r.source == "Workplace Gender Equality Agency (WGEA)"


def test_data_response_defaults():
    r = DataResponse(
        dataset_id="X", dataset_name="X",
        retrieved_at=datetime.now(timezone.utc),
        source_url="https://data.gov.au/",
    )
    assert r.row_count == 0
    assert r.records == []
    assert r.stale is False
    assert r.did_you_mean == []


def test_column_detail_role_default():
    c = ColumnDetail(key="k", source_column="k")
    assert c.role == "measure"


def test_dataset_detail_defaults():
    d = DatasetDetail(
        id="X", name="X", description="d",
        is_curated=True, source_url="https://data.gov.au/",
    )
    assert d.dimensions == []
    assert d.measures == []
