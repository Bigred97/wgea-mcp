"""Auto-discovery of the latest WGEA Public Data File ZIP on data.gov.au.

WGEA publishes one annual "WGEA Data — Public Data File" resource per
reporting year inside the single CKAN package `wgea-dataset`. The resource
URL embeds the year:

    https://data.gov.au/data/dataset/4d35cd80-2538-4705-82f3-d0d18e823d98/
    resource/<uuid>/download/wgea_public_dataset_2025.zip

`resolve_latest_zip(client)` calls CKAN `package_show?id=wgea-dataset`,
filters resources whose name matches "WGEA Data - Public Data File" and
whose format is ZIP/CSV, sorts by either the resource's `last_modified`
timestamp or the embedded reporting year, and returns the freshest entry.

Two-tier resolution:
  Tier 1  Live CKAN scrape  → CKAN package_show → newest matching resource.
                              Result cached as "catalog" for 6h.
  Tier 2  Bundled seed      → src/wgea_mcp/data/seed_urls.json shipped in
                              the wheel. Last-known-good URL per reporting
                              year. Used when Tier 1 returns no match.
                              Response is flagged stale=True with a reason.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from .client import WGEAAPIError, WGEAClient

WGEA_PACKAGE_ID = "wgea-dataset"
WGEA_CKAN_URL = f"https://data.gov.au/data/dataset/{WGEA_PACKAGE_ID}"

# Matches resource names like "2025 WGEA Data - Public Data File"
# Captures the 4-digit reporting year.
_PUBLIC_DATA_FILE_RE = re.compile(
    r"^(?P<year>\d{4})\s+WGEA\s+Data\s*-\s*Public\s+Data\s+File\s*$",
    re.IGNORECASE,
)

# A resource is considered "downloadable" if its format is one of these.
_DATA_FORMATS = {"zip", ".zip", "csv", ".csv", "xlsx", ".xlsx"}


@dataclass(frozen=True)
class WGEAResource:
    """One matched WGEA Public Data File resource from CKAN."""

    reporting_year_start: int  # e.g. 2025 → reporting year "2024-25" (Apr 2024 - Mar 2025)
    reporting_year_label: str  # canonical label "2024-25"
    url: str
    fmt: str
    size: int | None
    last_modified: datetime | None
    name: str


@dataclass(frozen=True)
class ResolvedZip:
    """Result of a discovery call."""

    url: str
    reporting_year_label: str
    reporting_year_start: int
    tier: str  # "ckan" | "seed"
    stale: bool = False
    reason: str | None = None


class DiscoveryError(Exception):
    """Raised by Tier-1 path when CKAN doesn't yield a usable resource."""


async def list_public_data_files(client: WGEAClient) -> list[WGEAResource]:
    """Return every "WGEA Data - Public Data File" resource on data.gov.au, newest first."""
    try:
        pkg = await client.fetch_package(WGEA_PACKAGE_ID)
    except WGEAAPIError as e:
        raise DiscoveryError(f"failed to fetch CKAN package {WGEA_PACKAGE_ID!r}: {e}") from e

    raw = pkg.get("resources")
    if not isinstance(raw, list):
        raise DiscoveryError(f"package {WGEA_PACKAGE_ID!r}: malformed resources field")

    out: list[WGEAResource] = []
    for res in raw:
        if not isinstance(res, dict):
            continue
        name = res.get("name")
        url = res.get("url")
        fmt = (res.get("format") or "").strip().lower()
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        m = _PUBLIC_DATA_FILE_RE.match(name.strip())
        if not m:
            continue
        if fmt and fmt not in _DATA_FORMATS:
            continue
        try:
            year = int(m.group("year"))
        except ValueError:
            continue
        size = res.get("size")
        if not isinstance(size, int):
            size = None
        last_mod = _parse_iso(res.get("last_modified")) or _parse_iso(res.get("created"))
        out.append(
            WGEAResource(
                reporting_year_start=year,
                reporting_year_label=_year_to_label(year),
                url=url,
                fmt=fmt or "zip",
                size=size,
                last_modified=last_mod,
                name=name.strip(),
            )
        )
    # Sort: newest reporting year first, ties broken by last_modified.
    out.sort(
        key=lambda r: (
            -r.reporting_year_start,
            -(r.last_modified.timestamp() if r.last_modified else 0.0),
        )
    )
    return out


async def resolve_latest_zip(client: WGEAClient) -> ResolvedZip:
    """Two-tier resolution. Never raises — returns ResolvedZip with stale flag."""
    # Tier 1: live CKAN
    try:
        all_res = await list_public_data_files(client)
        if all_res:
            top = all_res[0]
            return ResolvedZip(
                url=top.url,
                reporting_year_label=top.reporting_year_label,
                reporting_year_start=top.reporting_year_start,
                tier="ckan",
                stale=False,
            )
        ckan_err = "CKAN returned no matching Public Data File resources"
    except DiscoveryError as e:
        ckan_err = str(e)
    except Exception as e:  # pragma: no cover — defensive
        ckan_err = f"unexpected discovery error: {type(e).__name__}: {e}"

    # Tier 2: bundled seed manifest
    seed = load_seed_manifest()
    if seed:
        latest_year = max(seed.keys())
        seed_url = seed[latest_year]
        meta = seed_manifest_metadata()
        as_of = meta.get("refreshed_at") or meta.get("generated_at") or "unknown date"
        return ResolvedZip(
            url=seed_url,
            reporting_year_label=_year_to_label(latest_year),
            reporting_year_start=latest_year,
            tier="seed",
            stale=True,
            reason=(
                f"Live CKAN call failed ({ckan_err}); served from bundled seed "
                f"manifest (last verified {as_of})."
            ),
        )

    raise DiscoveryError(
        f"Live CKAN failed ({ckan_err}) and no seed manifest entry. "
        "Discovery has no fallback URL."
    )


async def resolve_for_year(client: WGEAClient, year_start: int) -> ResolvedZip:
    """Resolve the ZIP for a specific reporting year (year_start = 2025 → "2024-25").

    Useful for time-series compares ("2024 vs 2025"). Falls back to seed if
    CKAN is unreachable.
    """
    try:
        all_res = await list_public_data_files(client)
        for r in all_res:
            if r.reporting_year_start == year_start:
                return ResolvedZip(
                    url=r.url,
                    reporting_year_label=r.reporting_year_label,
                    reporting_year_start=r.reporting_year_start,
                    tier="ckan",
                    stale=False,
                )
        ckan_err = f"CKAN has no Public Data File for reporting year {year_start}"
    except DiscoveryError as e:
        ckan_err = str(e)

    seed = load_seed_manifest()
    if year_start in seed:
        meta = seed_manifest_metadata()
        as_of = meta.get("refreshed_at") or meta.get("generated_at") or "unknown date"
        return ResolvedZip(
            url=seed[year_start],
            reporting_year_label=_year_to_label(year_start),
            reporting_year_start=year_start,
            tier="seed",
            stale=True,
            reason=(
                f"Live CKAN call failed ({ckan_err}); served from bundled seed "
                f"manifest (last verified {as_of})."
            ),
        )
    raise DiscoveryError(ckan_err)


def _year_to_label(year_start: int) -> str:
    """Convert a 4-digit year-of-release to WGEA's "YYYY-YY" reporting-year label.

    WGEA's reporting year ends 31 March each year, so the "2025 Public Data File"
    covers 1 April 2024 - 31 March 2025 — labelled "2024-25".
    """
    prev = year_start - 1
    return f"{prev}-{str(year_start)[-2:]}"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # CKAN serves "2026-01-08T23:55:10.731609" — try ISO-8601 first.
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def load_seed_manifest() -> dict[int, str]:
    """Load the bundled seed_urls.json manifest.

    Returns {reporting_year_start: zip_url}. Empty dict if missing/malformed.
    """
    try:
        ref = resources.files("wgea_mcp").joinpath("data/seed_urls.json")
        text = ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        here = Path(__file__).resolve().parent / "data" / "seed_urls.json"
        if not here.is_file():
            return {}
        text = here.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    urls = data.get("urls")
    if not isinstance(urls, dict):
        return {}
    out: dict[int, str] = {}
    for k, v in urls.items():
        try:
            year = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            out[year] = v
    return out


def seed_manifest_metadata() -> dict[str, Any]:
    """Return the seed manifest's metadata block (refreshed_at etc.)."""
    try:
        ref = resources.files("wgea_mcp").joinpath("data/seed_urls.json")
        text = ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        here = Path(__file__).resolve().parent / "data" / "seed_urls.json"
        if not here.is_file():
            return {}
        text = here.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != "urls"}
