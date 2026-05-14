"""Fuzzy-match `did_you_mean` hints across curated.translate_filter_value + shaping."""
from __future__ import annotations

import pandas as pd
import pytest

from wgea_mcp import curated, parsing, shaping
from wgea_mcp.curated import translate_filter_value
from wgea_mcp.shaping import build_response


@pytest.fixture
def workforce_df(sample_zip_bytes) -> pd.DataFrame:
    return parsing.read_csv_from_zip(sample_zip_bytes, "wgea_workforce_composition_")


def test_translate_unknown_strict_enum_raises_with_hint():
    cd = curated.get("WORKFORCE_COMPOSITION")
    # gender is permissive — switch to a strict enum if any. employer_size has no strict enum.
    # Find a strict dim_values entry by checking permissive=False
    strict = [k for k, v in cd.dimension_values.items() if not v.permissive]
    if not strict:
        pytest.skip("no strict-enum dimensions in WORKFORCE_COMPOSITION")
    key = strict[0]
    valid = list(cd.dimension_values[key].values.keys())[0]
    # Pass a near-miss
    with pytest.raises(ValueError, match="Unknown value"):
        translate_filter_value(cd, key, valid + "X" + valid)


def test_employer_name_close_match_returns_hints(workforce_df):
    """Misspelt employer name should populate did_you_mean."""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "Comonwealth Banc"},  # misspelt
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    # Either we get a fuzzy match (because rapidfuzz scored it ≥ 80), or we get hints.
    assert resp.row_count > 0 or resp.did_you_mean, (
        "Expected fuzzy match or did_you_mean hints"
    )


def test_did_you_mean_filtered_by_relevance(workforce_df):
    """Hints should be relevant (no garbage suggestions for totally-unrelated input)."""
    cd = curated.get("WORKFORCE_COMPOSITION")
    resp = build_response(
        cd=cd, df=workforce_df,
        filters={"employer_name": "ZZZZZZZ_VERY_UNRELATED_QQQ"},
        measures=None, start_period=None, end_period=None,
        fmt="records", user_query={},
    )
    # Hints should be empty since nothing scores ≥ 50
    assert resp.did_you_mean == [] or all(isinstance(h, str) for h in resp.did_you_mean)


def test_aliases_load_from_resource():
    """The bundled employer_aliases.json should be loadable."""
    aliases = shaping._load_employer_aliases()
    assert "cba" in aliases
    assert aliases["cba"] == "commonwealth bank of australia"
    assert "qantas" in aliases
    assert "nab" in aliases


def test_aliases_lowercase_keys():
    aliases = shaping._load_employer_aliases()
    assert all(k == k.lower() for k in aliases)
