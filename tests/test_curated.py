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
    assert len(ids) == 8
    assert "WORKFORCE_COMPOSITION" in ids
    assert "WORKFORCE_MANAGEMENT" in ids
    assert "GENDER_EQUALITY_ACTIONS" in ids
    assert "PARENTAL_LEAVE_FLEX" in ids
    assert "HARM_PREVENTION" in ids
    assert "EMPLOYEE_SUPPORT" in ids
    assert "WORKPLACE_OVERVIEW" in ids
    assert "HEADLINE_GAP" in ids


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


# ─── HEADLINE_GAP schema tests ──────────────────────────────────────────

def test_headline_gap_schema():
    cd = curated.get("HEADLINE_GAP")
    assert cd is not None
    assert cd.format == "xlsx_aggregated"
    assert cd.layout == "wide"
    assert cd.cache_kind == "data"
    assert cd.period_column == "reporting_year"
    # xlsx_aggregated datasets need a stable download_url
    assert cd.download_url and cd.download_url.startswith("https://www.wgea.gov.au/")
    assert cd.download_url.endswith(".xlsx")
    # Source URL points at the WGEA report landing page
    assert cd.source_url.startswith("https://www.wgea.gov.au/")


def test_headline_gap_required_columns():
    cd = curated.get("HEADLINE_GAP")
    required = {
        "reporting_year",
        "anzsic_division",
        "total_remuneration_gap_pct",
        "base_salary_gap_pct",
        "median_total_rem_gap_pct",
        "median_base_salary_gap_pct",
        "employer_count",
    }
    assert required.issubset(cd.columns.keys()), (
        f"missing: {required - set(cd.columns.keys())}"
    )


def test_headline_gap_measures_have_unit():
    cd = curated.get("HEADLINE_GAP")
    pct_measures = [
        "total_remuneration_gap_pct",
        "base_salary_gap_pct",
        "median_total_rem_gap_pct",
        "median_base_salary_gap_pct",
    ]
    for k in pct_measures:
        col = cd.columns[k]
        assert col.role == "measure", k
        assert col.unit == "percent", f"{k} should have unit='percent', got {col.unit!r}"
        assert col.dtype == "float", f"{k} should have dtype='float', got {col.dtype!r}"
    emp = cd.columns["employer_count"]
    assert emp.role == "measure"
    assert emp.unit == "employers"
    assert emp.dtype == "integer"


def test_headline_gap_anzsic_division_is_permissive():
    cd = curated.get("HEADLINE_GAP")
    col = cd.columns["anzsic_division"]
    assert col.permissive is True
    # Dimension values map carries common aliases (letter, synonym, all)
    dv = cd.dimension_values["anzsic_division"]
    assert dv is not None and dv.values is not None
    assert dv.values["mining"] == "Mining"
    assert dv.values["b"] == "Mining"
    assert dv.values["finance"] == "Financial and Insurance Services"
    assert dv.values["national"] == "All employers"
    assert dv.values["all_employers"] == "All employers"


def test_headline_gap_anzsic_division_translates_letter_and_code():
    """aus_identity v0.3+ normalises an ANZSIC division letter or numeric code."""
    cd = curated.get("HEADLINE_GAP")
    assert translate_filter_value(cd, "anzsic_division", "B") == "Mining"
    # Numeric subdivision codes route through aus_identity too.
    assert translate_filter_value(cd, "anzsic_division", "06") == "Mining"
    assert translate_filter_value(cd, "anzsic_division", "0801") == "Mining"
    # Full canonical name passes through.
    assert (
        translate_filter_value(cd, "anzsic_division", "Mining") == "Mining"
    )


def test_headline_gap_anzsic_division_alias_resolves():
    """YAML alias map fires before aus_identity for non-ANZSIC synonyms."""
    cd = curated.get("HEADLINE_GAP")
    # 'banking' isn't a real ANZSIC name — only the alias map resolves it.
    assert (
        translate_filter_value(cd, "anzsic_division", "banking")
        == "Financial and Insurance Services"
    )
    assert (
        translate_filter_value(cd, "anzsic_division", "national")
        == "All employers"
    )


def test_headline_gap_anzsic_division_alias_case_insensitive():
    cd = curated.get("HEADLINE_GAP")
    # Aliases work regardless of case (mining / Mining / MINING).
    assert (
        translate_filter_value(cd, "anzsic_division", "MINING") == "Mining"
    )
    assert (
        translate_filter_value(cd, "anzsic_division", "Mining") == "Mining"
    )


def test_headline_gap_anzsic_division_unknown_passes_through():
    """Permissive dim — unknown values reach the matcher untranslated."""
    cd = curated.get("HEADLINE_GAP")
    # Made-up division. aus_identity rejects it; permissive=True keeps it.
    assert (
        translate_filter_value(cd, "anzsic_division", "Spaceflight")
        == "Spaceflight"
    )


def test_headline_gap_zip_member_is_sheet_name():
    """xlsx_aggregated datasets reuse `zip_member` as the sheet name."""
    cd = curated.get("HEADLINE_GAP")
    # Trailing space on the WGEA sheet name — exact match matters.
    assert cd.zip_member == "2. Employers "


def test_xlsx_aggregated_format_requires_download_url(tmp_path):
    """curated loader rejects xlsx_aggregated YAMLs without a download_url."""
    bad = tmp_path / "BAD.yaml"
    bad.write_text(
        "id: BAD\n"
        "name: bad\n"
        "description: bad\n"
        "source_url: https://example.com\n"
        "zip_member: sheet\n"
        "format: xlsx_aggregated\n"
        "layout: wide\n"
        "columns:\n"
        "  reporting_year:\n"
        "    source_column: reporting_year\n"
        "    role: dimension\n"
        "    dtype: string\n",
        encoding="utf-8",
    )
    # Manually invoke the single-file loader through internal helper.
    from wgea_mcp.curated import _load_one
    with pytest.raises(ValueError, match="download_url"):
        _load_one(bad)


def test_unsupported_format_rejected(tmp_path):
    bad = tmp_path / "BAD.yaml"
    bad.write_text(
        "id: BAD\n"
        "name: bad\n"
        "description: bad\n"
        "source_url: https://example.com\n"
        "zip_member: sheet\n"
        "format: parquet_partitioned\n"
        "layout: wide\n"
        "columns:\n"
        "  reporting_year:\n"
        "    source_column: reporting_year\n"
        "    role: dimension\n"
        "    dtype: string\n",
        encoding="utf-8",
    )
    from wgea_mcp.curated import _load_one
    with pytest.raises(ValueError, match="unsupported format"):
        _load_one(bad)


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


def test_all_per_employer_datasets_carry_employer_name():
    """The seven per-employer questionnaire/composition datasets all carry
    `employer_name`. HEADLINE_GAP is an aggregated dataset and intentionally
    drops the employer dimension (each row is an ANZSIC-division roll-up)."""
    for did in curated.list_ids():
        cd = curated.get(did)
        if cd.format == "xlsx_aggregated":
            # Aggregated rollups don't carry per-employer identity.
            continue
        assert "employer_name" in cd.columns, f"{did} missing employer_name"


def test_all_datasets_carry_reporting_year():
    for did in curated.list_ids():
        cd = curated.get(did)
        assert "reporting_year" in cd.columns, f"{did} missing reporting_year"


def test_all_datasets_source_url_is_wgea():
    """data.gov.au for the 7 csv_in_zip datasets, wgea.gov.au for HEADLINE_GAP."""
    for did in curated.list_ids():
        cd = curated.get(did)
        assert "wgea" in cd.source_url.lower() or "data.gov.au" in cd.source_url, (
            f"{did} source_url {cd.source_url!r} is not a WGEA-related host"
        )


def test_reset_registry_reloads():
    curated.list_ids()
    curated.reset_registry()
    # Should re-load on next access
    ids = curated.list_ids()
    assert len(ids) == 8
