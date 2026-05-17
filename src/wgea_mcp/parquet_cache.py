"""On-disk Parquet cache for parsed DataFrames.

The in-process LRU (`_df_cache` in `server.py`) handles warm queries in
~50ms but it's empty on cold restart — first call after a worker bounce
pays the full pandas/zipfile parse cost (13-22s for the largest WGEA
CSVs). The Parquet cache below persists the post-parse DataFrame to disk
so cold-restart loads complete in ~1-2s instead.

Location: defaults to `~/.wgea-mcp/parquet-cache/`, overridable via
`WGEA_MCP_PARQUET_CACHE_DIR` (used by tests + the Fly deploy that
mounts `/data/parquet-cache`).

TTL: 7 days, matching the SQLite byte-cache TTL for `kind="data"`.

Self-heal: a corrupted Parquet file is unlinked and the call falls
through to a fresh parse, matching the SQLite cache's corruption
recovery pattern.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd

# 7 days, matching cache.py's TTL for data-kind payloads.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60

_ENV_VAR = "WGEA_MCP_PARQUET_CACHE_DIR"
_DEFAULT_DIR = Path.home() / ".wgea-mcp" / "parquet-cache"


def cache_dir() -> Path:
    """Resolve the cache directory, creating it if needed."""
    override = os.environ.get(_ENV_VAR)
    path = Path(override) if override else _DEFAULT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key_to_filename(key: tuple[Any, ...]) -> str:
    """Hash a cache key tuple to a stable Parquet filename."""
    payload = repr(key).encode("utf-8")
    return hashlib.sha256(payload).hexdigest() + ".parquet"


def read_if_fresh(
    key: tuple[Any, ...], *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> pd.DataFrame | None:
    """Return the cached DataFrame if the Parquet file exists + is fresh.

    Returns None if missing, expired, or corrupted. Corrupted files are
    unlinked so the next call re-parses cleanly (same self-heal pattern
    as the SQLite byte-cache).
    """
    path = cache_dir() / _key_to_filename(key)
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ttl_seconds:
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        # Corrupted or partial-write — unlink and let the caller re-parse.
        try:
            path.unlink()
        except OSError:
            pass
        return None


def write(key: tuple[Any, ...], df: pd.DataFrame) -> None:
    """Persist a parsed DataFrame to the cache.

    Best-effort: filesystem failures are swallowed because the parsed
    DataFrame still works in-memory. Writes through a `.tmp` sibling +
    rename so a crash mid-write doesn't leave a corrupted file behind.
    """
    target = cache_dir() / _key_to_filename(key)
    tmp = target.with_suffix(".parquet.tmp")
    try:
        df.to_parquet(tmp, engine="pyarrow", compression="snappy", index=False)
        tmp.replace(target)
    except Exception:
        # Filesystem full, permission error, pyarrow choke on exotic dtypes,
        # etc. — never let cache-write errors break the user's request.
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass


def reset_for_tests() -> None:
    """Drop every cached file. For tests + manual recovery."""
    d = cache_dir()
    for f in d.glob("*.parquet"):
        try:
            f.unlink()
        except OSError:
            pass
    for f in d.glob("*.parquet.tmp"):
        try:
            f.unlink()
        except OSError:
            pass
