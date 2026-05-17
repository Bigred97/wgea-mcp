"""Parquet on-disk parsed-DataFrame cache (cold-restart speedup)."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from wgea_mcp import parquet_cache


@pytest.fixture(autouse=True)
def _isolate_parquet_dir(tmp_path, monkeypatch):
    """Every test gets its own cache dir; never touch the user's real one."""
    monkeypatch.setenv("WGEA_MCP_PARQUET_CACHE_DIR", str(tmp_path / "parquet"))
    yield


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "reporting_year": ["2024-25", "2024-25", "2024-25"],
            "employer_name": ["CBA", "Westpac", "NAB"],
            "n_employees": [50000, 40000, 35000],
        }
    )


def test_parquet_cache_writes_after_first_parse():
    """`write` persists the DataFrame; `read_if_fresh` round-trips it."""
    key = ("https://example.com/2025.zip", "WORKFORCE_COMPOSITION", "workforce.csv", 71_000_000, b"\x00" * 32)
    df_in = _sample_df()

    parquet_cache.write(key, df_in)
    df_out = parquet_cache.read_if_fresh(key)

    assert df_out is not None
    pd.testing.assert_frame_equal(df_in, df_out)


def test_parquet_cache_skips_csv_parse_on_warm_hit():
    """Distinct keys -> distinct files; collisions don't happen."""
    k1 = ("a", "WORKFORCE_COMPOSITION", 100, b"\x00")
    k2 = ("b", "EMPLOYEE_SUPPORT", 100, b"\x00")
    df1 = _sample_df()
    df2 = _sample_df().assign(employer_name=["x", "y", "z"])

    parquet_cache.write(k1, df1)
    parquet_cache.write(k2, df2)

    out1 = parquet_cache.read_if_fresh(k1)
    out2 = parquet_cache.read_if_fresh(k2)
    assert out1 is not None and out2 is not None
    assert list(out1["employer_name"]) == ["CBA", "Westpac", "NAB"]
    assert list(out2["employer_name"]) == ["x", "y", "z"]


def test_parquet_cache_self_heals_on_corruption(tmp_path, monkeypatch):
    """A corrupted Parquet file is unlinked + treated as a cache miss."""
    monkeypatch.setenv("WGEA_MCP_PARQUET_CACHE_DIR", str(tmp_path / "p"))
    key = ("u", "EMPLOYEE_SUPPORT", 1, b"\xff")
    # Write a known-good file first so we know the path resolution works.
    parquet_cache.write(key, _sample_df())
    cache_path = parquet_cache.cache_dir() / parquet_cache._key_to_filename(key)
    assert cache_path.is_file()

    # Truncate to corrupt the Parquet magic bytes.
    cache_path.write_bytes(b"not a real parquet file")
    assert cache_path.is_file()

    result = parquet_cache.read_if_fresh(key)
    assert result is None  # missing-after-corruption signal
    assert not cache_path.is_file()  # self-heal unlinked the bad file


def test_parquet_cache_respects_ttl(tmp_path, monkeypatch):
    """File older than ttl_seconds returns None even if readable."""
    monkeypatch.setenv("WGEA_MCP_PARQUET_CACHE_DIR", str(tmp_path / "p"))
    key = ("ttl-test", "WORKFORCE_COMPOSITION", 1, b"")
    parquet_cache.write(key, _sample_df())

    cache_path = parquet_cache.cache_dir() / parquet_cache._key_to_filename(key)
    # Backdate mtime to 8 days ago.
    old = time.time() - 8 * 24 * 60 * 60
    import os

    os.utime(cache_path, (old, old))

    result = parquet_cache.read_if_fresh(key, ttl_seconds=7 * 24 * 60 * 60)
    assert result is None  # expired
    # File itself is not deleted on TTL expiry — only on corruption.
    assert cache_path.is_file()

    # And with a permissive TTL it comes back.
    result2 = parquet_cache.read_if_fresh(key, ttl_seconds=30 * 24 * 60 * 60)
    assert result2 is not None


def test_parquet_cache_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("WGEA_MCP_PARQUET_CACHE_DIR", str(tmp_path / "p"))
    assert parquet_cache.read_if_fresh(("never-written",)) is None


def test_env_var_override_routes_writes(tmp_path, monkeypatch):
    """WGEA_MCP_PARQUET_CACHE_DIR redirects the on-disk location."""
    target = tmp_path / "custom-cache"
    monkeypatch.setenv("WGEA_MCP_PARQUET_CACHE_DIR", str(target))
    parquet_cache.write(("env-test",), _sample_df())
    files = list(target.glob("*.parquet"))
    assert len(files) == 1


def test_reset_for_tests_clears_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WGEA_MCP_PARQUET_CACHE_DIR", str(tmp_path / "p"))
    parquet_cache.write(("k1",), _sample_df())
    parquet_cache.write(("k2",), _sample_df())
    parquet_cache.reset_for_tests()
    assert list(parquet_cache.cache_dir().glob("*.parquet")) == []
