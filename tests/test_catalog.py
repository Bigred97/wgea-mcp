"""Fuzzy search across the curated dataset catalog."""
from __future__ import annotations

import pytest

from wgea_mcp import catalog


def test_search_pay_gap_top_hit_is_gender_equality_actions():
    hits = catalog.search("pay gap analysis")
    assert len(hits) > 0
    assert hits[0].id == "GENDER_EQUALITY_ACTIONS"


def test_search_parental_leave_top_hit():
    hits = catalog.search("parental leave")
    assert hits[0].id == "PARENTAL_LEAVE_FLEX"


def test_search_harassment_top_hit():
    hits = catalog.search("sexual harassment")
    assert hits[0].id == "HARM_PREVENTION"


def test_search_workforce_top_hit():
    hits = catalog.search("workforce composition")
    assert hits[0].id == "WORKFORCE_COMPOSITION"


def test_search_promotions_finds_workforce_management():
    hits = catalog.search("promotions women")
    assert hits[0].id == "WORKFORCE_MANAGEMENT"


def test_search_flexible_work_finds_parental_leave():
    hits = catalog.search("flexible work from home")
    assert hits[0].id == "PARENTAL_LEAVE_FLEX"


def test_search_eap_finds_employee_support():
    hits = catalog.search("employee assistance program")
    assert hits[0].id == "EMPLOYEE_SUPPORT"


def test_search_respects_limit():
    hits = catalog.search("anything", limit=2)
    assert len(hits) == 2


def test_search_empty_query_raises():
    with pytest.raises(ValueError):
        catalog.search("")


def test_search_whitespace_only_raises():
    with pytest.raises(ValueError):
        catalog.search("   ")


def test_list_summaries_returns_all_curated():
    summaries = catalog.list_summaries()
    assert len(summaries) == 8
    assert all(s.is_curated for s in summaries)
    assert all(s.update_frequency == "annual" for s in summaries)
