"""Curated registry — YAML loading, lookup helpers, filter translation."""
from __future__ import annotations

import pytest

from wgea_mcp import curated
from wgea_mcp.curated import (
    dimension_columns,
    id_columns,
    measure_columns,
    resolve_measure_keys,
    translate_filter_value,
)


def test_list_ids_returns_all_curated():
    ids = curated.list_ids()
    assert len(ids) == 7
    assert "WORKFORCE_COMPOSITION" in ids
    assert "WORKFORCE_MANAGEMENT" in ids
    assert "GENDER_EQUALITY_ACTIONS" in ids
    assert "PARENTAL_LEAVE_FLEX" in ids
    assert "HARM_PREVENTION" in ids
    assert "EMPLOYEE_SUPPORT" in ids
    assert "WORKPLACE_OVERVIEW" in ids


def test_list_ids_is_sorted():
    ids = curated.list_ids()
    assert ids == sorted(ids)


def test_get_is_case_insensitive():
    cd1 = curated.get("workforce_composition")
    cd2 = curated.get("WORKFORCE_COMPOSITION")
    cd3 = curated.get("Workforce_Composition")
    assert cd1 is not None
    assert cd1.id == cd2.id == cd3.id == "WORKFORCE_COMPOSITION"


def test_get_unknown_returns_none():
    assert curated.get("BOGUS") is None
    assert curated.get("") is None


def test_workforce_composition_schema():
    cd = curated.get("WORKFORCE_COMPOSITION")
    assert cd is not None
    assert cd.format == "csv_in_zip"
    assert cd.layout == "wide"
    assert cd.zip_member.startswith("wgea_workforce_composition")
    assert cd.period_column == "reporting_year"
    # Required columns present
    keys = set(cd.columns.keys())
    for required in ["employer_name", "employer_abn", "gender", "occupation", "n_employees"]:
        assert required in keys


def test_workforce_composition_has_measures_and_dimensions():
    cd = curated.get("WORKFORCE_COMPOSITION")
    measures = measure_columns(cd)
    dims = dimension_columns(cd)
    ids = id_columns(cd)
    assert len(measures) >= 1
    assert all(m.role == "measure" for m in measures)
    assert len(dims) >= 5
    assert all(d.role == "dimension" for d in dims)
    assert all(i.role == "id" for i in ids)


def test_workforce_composition_n_employees_is_integer():
    cd = curated.get("WORKFORCE_COMPOSITION")
    n_emp = cd.columns.get("n_employees")
    assert n_emp is not None
    assert n_emp.dtype == "integer"
    assert n_emp.unit == "employees"


def test_employer_name_is_permissive():
    cd = curated.get("WORKFORCE_COMPOSITION")
    assert cd.columns["employer_name"].permissive is True


def test_questionnaire_datasets_have_response_dim():
    for did in ("GENDER_EQUALITY_ACTIONS", "HARM_PREVENTION", "PARENTAL_LEAVE_FLEX",
                "EMPLOYEE_SUPPORT", "WORKPLACE_OVERVIEW"):
        cd = curated.get(did)
        assert cd is not None, did
        assert "response" in cd.columns, did
        assert cd.layout == "long", did


def test_translate_filter_value_alias_to_canonical():
    cd = curated.get("WORKFORCE_COMPOSITION")
    canonical = translate_filter_value(cd, "gender", "women")
    assert canonical == "Women"


def test_translate_filter_value_canonical_passthrough():
    cd = curated.get("WORKFORCE_COMPOSITION")
    canonical = translate_filter_value(cd, "gender", "Women")
    assert canonical == "Women"


def test_translate_filter_value_permissive_unknown_passes():
    cd = curated.get("WORKFORCE_COMPOSITION")
    # gender is permissive — unknown values pass through unchanged
    result = translate_filter_value(cd, "gender", "Other")
    assert result == "Other"


def test_translate_filter_value_no_enum_passes_through():
    cd = curated.get("WORKFORCE_COMPOSITION")
    # employer_name has no enum — anything passes
    result = translate_filter_value(cd, "employer_name", "literally anything")
    assert result == "literally anything"


def test_resolve_measure_keys_none_returns_all():
    cd = curated.get("WORKFORCE_COMPOSITION")
    all_measures = [c.key for c in measure_columns(cd)]
    assert sorted(resolve_measure_keys(cd, None)) == sorted(all_measures)


def test_resolve_measure_keys_string_single():
    cd = curated.get("WORKFORCE_COMPOSITION")
    out = resolve_measure_keys(cd, "n_employees")
    assert out == ["n_employees"]


def test_resolve_measure_keys_unknown_raises():
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match="Unknown measure"):
        resolve_measure_keys(cd, "nonexistent")


def test_resolve_measure_keys_empty_list_raises():
    cd = curated.get("WORKFORCE_COMPOSITION")
    with pytest.raises(ValueError, match="empty list"):
        resolve_measure_keys(cd, [])


def test_resolve_measure_keys_deduplicates():
    cd = curated.get("WORKFORCE_COMPOSITION")
    out = resolve_measure_keys(cd, ["n_employees", "n_employees"])
    assert out == ["n_employees"]


def test_all_datasets_carry_employer_name():
    for did in curated.list_ids():
        cd = curated.get(did)
        assert "employer_name" in cd.columns, f"{did} missing employer_name"


def test_all_datasets_carry_reporting_year():
    for did in curated.list_ids():
        cd = curated.get(did)
        assert "reporting_year" in cd.columns, f"{did} missing reporting_year"


def test_all_datasets_source_url_is_data_gov_au():
    for did in curated.list_ids():
        cd = curated.get(did)
        assert "data.gov.au" in cd.source_url


def test_reset_registry_reloads():
    curated.list_ids()
    curated.reset_registry()
    # Should re-load on next access
    ids = curated.list_ids()
    assert len(ids) == 7
