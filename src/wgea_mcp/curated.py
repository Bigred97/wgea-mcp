"""Hand-curated metadata for the WGEA public data file's six CSVs.

Each YAML under `data/curated/` describes one queryable table:
- which CSV member inside the annual WGEA ZIP it maps to
- which columns are dimensions (filterable) vs measures (returned values)
- plain-English aliases for verbose source column names
- which filter values are accepted, what they mean
- search keywords folded into the fuzzy search haystack
- (employer dimension only) `permissive: true` so any free-text employer
  name reaches the rapidfuzz layer for "did you mean?" matching
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml

Layout = Literal["wide", "long"]


@dataclass(frozen=True)
class CuratedColumn:
    """One column in the source CSV that's exposed to users."""

    key: str
    source_column: str
    description: str | None = None
    unit: str | None = None
    role: str = "measure"  # "dimension" | "measure" | "id"
    dtype: str | None = None
    permissive: bool = False
    # Value applied as an implicit filter when the caller omits this
    # dimension from `filters` entirely. Only meaningful for dimension
    # columns whose source data carries an explicit "any value" sentinel
    # row (e.g. HEADLINE_GAP's `employer_size_band` = "all"). Without this,
    # omitting the filter left every fine-grained fragment unfiltered,
    # which could silently push the true aggregate row out of a capped
    # response — see 0.6.x employer_size_band default-fragment bug.
    default_value: str | None = None


@dataclass(frozen=True)
class CuratedDimensionValues:
    """Allowed values for a dimension, plus their canonical labels."""

    values: dict[str, str] | None = None
    permissive: bool = False


@dataclass(frozen=True)
class CuratedDataset:
    """One curated dataset (a single queryable view onto one source file)."""

    id: str
    name: str
    description: str
    source_url: str  # WGEA / data.gov.au landing
    # For csv_in_zip datasets (the 7 questionnaire+composition tables) this is
    # the filename or prefix inside the annual Public Data File ZIP. For
    # xlsx_aggregated (HEADLINE_GAP) it's the sheet name inside the xlsx.
    zip_member: str
    # csv_in_zip:     fetched via data.gov.au CKAN discovery, parsed as one
    #                 CSV per dataset out of the annual WGEA Public Data File
    # xlsx_aggregated: fetched directly from a stable `download_url` on
    #                 wgea.gov.au, then aggregated server-side to a small
    #                 (<50 row) DataFrame before shaping
    format: Literal["csv_in_zip", "xlsx_aggregated"]
    layout: Layout
    period_coverage: str | None
    update_frequency: str | None
    cache_kind: str  # "data"
    columns: dict[str, CuratedColumn]
    dimension_values: dict[str, CuratedDimensionValues]
    search_keywords: tuple[str, ...] = ()
    # The reporting-year column in the source CSV. Every WGEA CSV has a
    # `reporting_year` column ("2024-25", "2023-24", ...). When the same
    # dataset spans multiple years, agents can filter on this.
    period_column: str | None = "reporting_year"
    # Direct fetch URL for xlsx_aggregated datasets. csv_in_zip datasets
    # leave this None — they resolve URLs at request time via CKAN discovery.
    download_url: str | None = None
    # Comparability caveat surfaced on DataResponse (not just describe()
    # prose) whenever a query is scoped to a specific `anzsic_division`
    # value other than the dataset's national "All employers" row. Only
    # HEADLINE_GAP sets this today — its per-industry employee-weighted
    # figures use a different weighting methodology than the national
    # headline and are not directly comparable to it. None means "no
    # caveat applies to this dataset".
    industry_caveat: str | None = None


_REGISTRY: dict[str, CuratedDataset] | None = None


def _yaml_dir() -> Path:
    try:
        ref = resources.files("wgea_mcp").joinpath("data/curated")
        if ref.is_dir():
            return Path(str(ref))
    except (ModuleNotFoundError, AttributeError):
        pass
    here = Path(__file__).resolve().parent / "data" / "curated"
    if here.is_dir():
        return here
    raise FileNotFoundError("Could not locate wgea_mcp/data/curated/")


def _parse_column(key: str, raw: dict) -> CuratedColumn:
    if not isinstance(raw, dict):
        raise ValueError(f"Column {key!r} must be a mapping, got {type(raw).__name__}")
    if "source_column" not in raw:
        raise ValueError(f"Column {key!r} missing required field 'source_column'")
    default_value = raw.get("default_value")
    return CuratedColumn(
        key=key,
        source_column=str(raw["source_column"]),
        description=raw.get("description"),
        unit=raw.get("unit"),
        role=str(raw.get("role", "measure")),
        dtype=raw.get("dtype"),
        permissive=bool(raw.get("permissive", False)),
        default_value=str(default_value) if default_value is not None else None,
    )


def _parse_dimension_values(raw) -> CuratedDimensionValues:
    if raw is None:
        return CuratedDimensionValues(values=None)
    if isinstance(raw, dict):
        if "values" in raw and isinstance(raw["values"], dict):
            vals = {str(k): str(v) for k, v in raw["values"].items()}
            return CuratedDimensionValues(
                values=vals, permissive=bool(raw.get("permissive", False))
            )
        return CuratedDimensionValues(
            values={str(k): str(v) for k, v in raw.items()}, permissive=False
        )
    raise ValueError(f"dimension_values must be a mapping, got {type(raw).__name__}")


def _load_one(path: Path) -> CuratedDataset:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: top-level must be a mapping")

    columns: dict[str, CuratedColumn] = {}
    for key, col_raw in (raw.get("columns") or {}).items():
        columns[key] = _parse_column(key, col_raw)

    dim_values: dict[str, CuratedDimensionValues] = {}
    for key, val_raw in (raw.get("dimension_values") or {}).items():
        dim_values[key] = _parse_dimension_values(val_raw)

    fmt = str(raw.get("format", "csv_in_zip")).lower()
    if fmt not in ("csv_in_zip", "xlsx_aggregated"):
        raise ValueError(
            f"{path.name}: unsupported format {fmt!r}. "
            "Supported: 'csv_in_zip' (annual Public Data File), "
            "'xlsx_aggregated' (rolled-up spreadsheet on wgea.gov.au)."
        )

    layout = str(raw.get("layout", "wide")).lower()
    if layout not in ("wide", "long"):
        raise ValueError(f"{path.name}: layout must be 'wide' or 'long', got {layout!r}")

    zip_member = raw.get("zip_member")
    if not zip_member:
        raise ValueError(f"{path.name}: missing required field 'zip_member'")

    download_url = raw.get("download_url")
    if fmt == "xlsx_aggregated":
        if not download_url:
            raise ValueError(
                f"{path.name}: 'xlsx_aggregated' format requires a 'download_url' "
                "pointing at the published spreadsheet."
            )
        if not str(download_url).startswith(("http://", "https://")):
            raise ValueError(
                f"{path.name}: 'download_url' must be an http(s) URL, "
                f"got {download_url!r}"
            )

    return CuratedDataset(
        id=str(raw["id"]),
        name=str(raw["name"]),
        description=str(raw.get("description", "")),
        source_url=str(raw["source_url"]),
        zip_member=str(zip_member),
        format=fmt,  # type: ignore[arg-type]
        layout=layout,  # type: ignore[arg-type]
        period_coverage=raw.get("period_coverage"),
        update_frequency=raw.get("update_frequency"),
        cache_kind=str(raw.get("cache_kind", "data")),
        columns=columns,
        dimension_values=dim_values,
        search_keywords=tuple(raw.get("search_keywords") or ()),
        period_column=raw.get("period_column", "reporting_year"),
        download_url=str(download_url) if download_url else None,
        industry_caveat=(
            str(raw["industry_caveat"]).strip()
            if raw.get("industry_caveat")
            else None
        ),
    )


def _load_all() -> dict[str, CuratedDataset]:
    out: dict[str, CuratedDataset] = {}
    for path in sorted(_yaml_dir().glob("*.yaml")):
        cd = _load_one(path)
        if cd.id in out:
            raise ValueError(f"Duplicate curated id {cd.id!r} (from {path.name})")
        out[cd.id] = cd
    return out


def get(dataset_id: str) -> CuratedDataset | None:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_all()
    return _REGISTRY.get(dataset_id.upper())


def list_ids() -> list[str]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_all()
    return sorted(_REGISTRY.keys())


def list_all() -> list[CuratedDataset]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_all()
    return [_REGISTRY[k] for k in sorted(_REGISTRY.keys())]


def reset_registry() -> None:
    """For tests."""
    global _REGISTRY
    _REGISTRY = None


def dimension_columns(cd: CuratedDataset) -> list[CuratedColumn]:
    return [c for c in cd.columns.values() if c.role == "dimension"]


def measure_columns(cd: CuratedDataset) -> list[CuratedColumn]:
    return [c for c in cd.columns.values() if c.role == "measure"]


def id_columns(cd: CuratedDataset) -> list[CuratedColumn]:
    return [c for c in cd.columns.values() if c.role == "id"]


def translate_filter_value(
    cd: CuratedDataset, dim_key: str, user_value: str
) -> str:
    """Translate a user-supplied dimension value to the source-column value.

    If the dim has an enumerated `dimension_values` map, the user can pass
    either an alias ('cba') or the canonical value ('Commonwealth Bank...').
    Free-form (no enum) and permissive dims pass values through unchanged.
    Unknown values on a strict (non-permissive) enum raise ValueError with
    a "Did you mean?" suggestion when a near-match exists.

    The `anzsic_division` dimension gets a second-tier normaliser via
    aus_identity (v0.3.0+) so a user can pass the canonical name
    ('Mining'), the division letter ('B'), a 2/3/4-digit ANZSIC code
    ('06', '0801'), or a synonym from the YAML alias map and land on
    the same canonical division name. ANZSIC division names are
    cross-source standard — every sister MCP that exposes industry-level
    data normalises through aus_identity.
    """
    dv = cd.dimension_values.get(dim_key)
    # Case-insensitive lookup against alias keys so 'mining' resolves
    # whether the YAML stores 'mining' or the user passed 'Mining'.
    user_lower = user_value.strip().lower() if isinstance(user_value, str) else user_value
    if dv is not None and dv.values is not None:
        if user_value in dv.values:
            return dv.values[user_value]
        if user_lower in dv.values:
            return dv.values[user_lower]
        if user_value in dv.values.values():
            return user_value

    # Cross-sister ANZSIC division normaliser. Only fires when the dim is
    # named `anzsic_division` and the YAML alias map didn't already
    # resolve the value. Lets a caller pass 'B' / '06' / 'mining' and
    # land on 'Mining' (the canonical name WGEA uses in the xlsx).
    if dim_key == "anzsic_division":
        canonical = _normalize_anzsic_division_safe(user_value)
        if canonical is not None:
            return canonical

    if dv is not None and dv.permissive:
        return user_value
    if dv is None or dv.values is None:
        return user_value
    valid = sorted(dv.values.keys())
    hint = _did_you_mean(user_value, valid)
    suggestion = f" Did you mean {hint!r}?" if hint else ""
    raise ValueError(
        f"Unknown value {user_value!r} for filter {dim_key!r} on dataset {cd.id!r}."
        f"{suggestion} Try one of: {', '.join(valid[:15])}"
        + ("..." if len(valid) > 15 else "")
    )


def _normalize_anzsic_division_safe(user_value: str) -> str | None:
    """Try aus_identity's ANZSIC division normaliser; None on no-match.

    aus_identity 0.3+ exposes `ANZSIC_DIVISIONS` (letter → canonical name)
    and `normalize_anzsic_division` which accepts letters, names, or codes.
    Wrapped so a bad input doesn't escape — the caller falls back to the
    permissive path so wgea-mcp still answers 'unknown industry' queries
    with a sensible response rather than a hard error.
    """
    if not isinstance(user_value, str) or not user_value.strip():
        return None
    try:
        from aus_identity import ANZSIC_DIVISIONS, normalize_anzsic_division
    except ImportError:
        return None
    try:
        letter = normalize_anzsic_division(user_value)
    except ValueError:
        return None
    return ANZSIC_DIVISIONS.get(letter)


def _did_you_mean(user_value: str, candidates: list[str]) -> str | None:
    """Closest candidate string if it scores ≥ 70 on RapidFuzz WRatio."""
    if not candidates:
        return None
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return None
    match = process.extractOne(user_value, candidates, scorer=fuzz.WRatio, score_cutoff=70)
    return match[0] if match else None


def resolve_measure_keys(
    cd: CuratedDataset, requested: str | list[str] | None
) -> list[str]:
    """Translate a user's measures= request into a list of measure keys."""
    measure_keys = [c.key for c in measure_columns(cd)]
    if requested is None:
        return measure_keys
    if isinstance(requested, str):
        items = [requested]
    elif isinstance(requested, list):
        if not requested:
            raise ValueError(
                "measures filter is an empty list. "
                "Pass at least one measure, or omit `measures` to return all."
            )
        items = [str(x) for x in requested]
    else:
        raise ValueError(
            f"measures must be a string or list of strings, got {type(requested).__name__}."
        )

    source_to_key = {
        c.source_column: c.key for c in cd.columns.values() if c.role == "measure"
    }
    valid_keys = set(measure_keys)
    out: list[str] = []
    for v in items:
        v_str = v.strip()
        if not v_str:
            raise ValueError(
                f"Empty measure key. Try one of: {', '.join(sorted(valid_keys)[:15])}"
            )
        if v_str in valid_keys:
            out.append(v_str)
        elif v_str in source_to_key:
            out.append(source_to_key[v_str])
        else:
            valid_hint = (
                ", ".join(sorted(valid_keys)[:15])
                if valid_keys
                else "(none — dataset has no curated measures)"
            )
            raise ValueError(
                f"Unknown measure {v!r} for dataset {cd.id!r}. "
                f"Try one of: {valid_hint}"
                + ("..." if len(valid_keys) > 15 else "")
            )
    seen: set[str] = set()
    return [k for k in out if not (k in seen or seen.add(k))]
