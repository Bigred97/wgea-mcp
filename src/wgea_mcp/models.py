"""Pydantic v2 response models for wgea-mcp.

Mirrors the response envelope used by abs-mcp / rba-mcp / ato-mcp / apra-mcp /
aihw-mcp / asic-mcp so a downstream agent that calls multiple Australian
government MCPs gets a uniform shape.

WGEA-specific differences:
- attribution names WGEA and CC-BY 3.0 AU.
- DataResponse.source defaults to "Workplace Gender Equality Agency (WGEA)"
- DataResponse.source_url points at the WGEA data.gov.au landing page
- DataResponse.download_url surfaces the actual ZIP URL used (post-discovery)
- DataResponse.reporting_year — the WGEA reporting year (e.g. "2024-25") the
  rows came from. WGEA releases one new reporting year per annual cycle.
- DataResponse.stale + stale_reason — true when the live CKAN call failed
  and we served from the bundled seed manifest.
- DataResponse.caveat — comparability warning surfaced on the response
  itself (mirrors apra-mcp's `framework` pattern) when a query is scoped
  to a dimension value that isn't directly comparable to the dataset's
  national/headline figure, e.g. HEADLINE_GAP's per-industry cut.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


_WGEA_ATTRIBUTION = (
    "Source: Workplace Gender Equality Agency. "
    "Licensed under Creative Commons Attribution 3.0 Australia "
    "(https://creativecommons.org/licenses/by/3.0/au/). "
    "Original dataset: https://data.gov.au/data/dataset/wgea-dataset"
)


class DatasetSummary(BaseModel):
    """Search-result shape: one row per curated WGEA dataset."""

    id: str
    name: str
    description: str | None = None
    update_frequency: str | None = None  # "annual"
    is_curated: bool = False


class ColumnDetail(BaseModel):
    """One queryable column in a curated table."""

    key: str
    source_column: str
    description: str | None = None
    unit: str | None = None
    role: str = "measure"  # "dimension" | "measure" | "id"


class DatasetDetail(BaseModel):
    """describe_dataset shape."""

    id: str
    name: str
    description: str
    is_curated: bool
    update_frequency: str | None = None
    period_coverage: str | None = None
    dimensions: list[ColumnDetail] = Field(default_factory=list)
    measures: list[ColumnDetail] = Field(default_factory=list)
    source_url: str
    download_url: str | None = None
    reporting_year_latest: str | None = None  # e.g. "2024-25"


class Observation(BaseModel):
    """One row of returned data."""

    reporting_year: str | None = None  # e.g. "2024-25"
    # 0.6.14: also surface `period` as a portfolio-uniform alias for
    # reporting_year. Cross-sister consumers (the ausdata-api gateway,
    # tools that iterate any DataResponse) expect a `period` field on
    # every Observation. WGEA's annual cadence makes period == reporting_
    # year exactly — the field is populated mirror-style at construction
    # time via shaping.py.
    period: str | None = None
    value: float | None = None
    measure: str | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)
    unit: str | None = None


class DataResponse(BaseModel):
    """get_data / latest shape — uniform across curated datasets.

    `records` carries either:
      - list of `Observation` (default "records" format), or
      - list of dicts shaped {measure, unit, observations: [...]}.
    """

    dataset_id: str
    dataset_name: str
    query: dict[str, Any] = Field(default_factory=dict)
    reporting_year: str | None = None  # latest reporting_year covered by this response
    period: dict[str, str | None] = Field(
        default_factory=lambda: {"start": None, "end": None},
        description=(
            "Canonical period bounds {start, end} for cross-sister consumers. "
            "Populated alongside the wgea-specific reporting_year. For a single "
            "reporting year both bounds match; for multi-year spans they bracket "
            "the range."
        ),
    )
    unit: str | None = None
    row_count: int = 0
    records: list[Any] = Field(default_factory=list)
    csv: str | None = None
    source: str = "Workplace Gender Equality Agency (WGEA)"
    attribution: str = _WGEA_ATTRIBUTION
    retrieved_at: datetime
    source_url: str  # canonical WGEA data.gov.au landing page
    download_url: str | None = None  # actual ZIP URL used (post-discovery)
    did_you_mean: list[str] = Field(default_factory=list)  # fuzzy-match hints
    stale: bool = False
    stale_reason: str | None = None
    # Set when the query is scoped to a specific dimension value that is NOT
    # directly comparable to the dataset's headline/national figure (e.g.
    # HEADLINE_GAP's per-`anzsic_division` employee-weighted GPG vs the
    # national "All employers" figure — different weighting methodology).
    # Carried on the response itself (not just describe() prose) so a caller
    # who never reads the docs still sees the warning where the data lands.
    caveat: str | None = None
    # Set when `latest()` (or get_data's max_rows cap) truncated a larger
    # post-filter result. Original row count goes here so agents can detect
    # + surface the cap.
    truncated_at: int | None = None
    server_version: str = Field(default_factory=lambda: _get_server_version())


def _get_server_version() -> str:
    try:
        from importlib.metadata import version

        return version("wgea-mcp")
    except Exception:
        return "0.0.0+unknown"
