# wgea-mcp

Sister MCP in the Australian Public Data stack. See `../CLAUDE.md` for
portfolio-wide conventions; this file captures repo-specific details
plus the cross-sister discipline.

## Source

| | |
|--|--|
| Source agency | Workplace Gender Equality Agency (WGEA) |
| Source URL | https://data.gov.au/data/dataset/wgea-dataset |
| Data format | Annual ZIP-of-CSVs (Public Data File, ~71 MB) via data.gov.au CKAN |
| Licence | CC-BY 3.0 Australia |
| Licence URL | https://creativecommons.org/licenses/by/3.0/au/ |
| Python module | `wgea_mcp` |
| PyPI package | `wgea-mcp` |
| GitHub | https://github.com/Bigred97/wgea-mcp |

## Curated datasets (8)

WORKFORCE_COMPOSITION · WORKFORCE_MANAGEMENT · GENDER_EQUALITY_ACTIONS ·
PARENTAL_LEAVE_FLEX · HARM_PREVENTION · EMPLOYEE_SUPPORT · WORKPLACE_OVERVIEW ·
HEADLINE_GAP

HEADLINE_GAP is the odd one out — it's `format: xlsx_aggregated`, sourced
from a stable URL on wgea.gov.au (not data.gov.au), and aggregated
server-side to ~20 rows (19 ANZSIC divisions + 1 synthetic "All employers"
national row). The other 7 are `format: csv_in_zip` and route through the
Public Data File ZIP discovery on data.gov.au.

## Repo-specific module set

Required (every sister): `server.py`, `models.py`, `curated.py`, `client.py`, `cache.py`, `shaping.py`, `data/curated/*.yaml`

Repo-specific extras:
- `parsing.py` — CSV-from-ZIP extraction (WGEA ships one annual ZIP containing the seven thematic CSVs)
- `discovery.py` — 2-tier resolver: live CKAN `package_show` for the newest Public Data File, fallback to bundled `data/seed_urls.json` manifest
- `catalog.py` — search ranking across the seven curated datasets

## Repo-specific gotchas

- **HEADLINE_GAP is the only `xlsx_aggregated` dataset in the portfolio.**
  Source URL is a stable WGEA path (`Employer-Gender-Pay-Gaps-Spreadsheet.xlsx`
  on `wgea.gov.au`), not data.gov.au CKAN. The client whitelist accepts
  both hosts. The aggregator filters to private-sector rows only, mirroring
  WGEA's published Figure 4 mid-points. Reporting year is extracted from
  the xlsx title row (cell row 2 col 0). Do NOT route this dataset through
  the streaming/CKAN path — `_get_data_impl` forks on `cd.format`.
- **`anzsic_division` filter normalises through `aus_identity>=0.3.0`.**
  Users can pass full division name, ANZSIC letter (A-S), or 2/3/4-digit
  numeric code; YAML alias map also handles synonyms ('banking', 'realestate',
  'all'). Permissive=True keeps unknown values flowing to the matcher.
- **Workflow file is named `publish.yml` (NOT `release.yml` like sister MCPs).**
  PyPI Trusted Publishing has a pending-publisher pointed at the filename
  `publish.yml`; renaming would break OIDC auth on the next release. Leave
  as-is. Same shape as the sister `release.yml` files (build → wheel-verify
  → OIDC publish to PyPI on tag/release).
- The discovery layer has its own stale-flag path: when live CKAN fails,
  `resolve_latest_zip` falls back to `data/seed_urls.json` and sets
  `stale=True, reason="Live CKAN call failed ..."`. The 0.1.3 client-level
  stale-fallback (graceful degradation per CLAUDE.md dim #4) layers
  underneath — if the seed URL itself can't be downloaded, the cached ZIP
  bytes are served from SQLite with a separate stale_reason. `_get_data_impl`
  in `server.py` merges both signals (discovery-level takes precedence).
- WGEA reporting years are labelled "YYYY-YY" (e.g. "2024-25" = 1 Apr 2024
  to 31 Mar 2025). Lexical string compare works for ordering — no datetime
  conversion needed.
- Employer-name fuzzy match uses a combined `WRatio + partial_ratio` scorer
  (not WRatio alone — see 0.1.2 CHANGELOG: WRatio alone tied "Commonweath
  Bank" between Commonwealth and Bendigo). Threshold 75 after the blend.
- `~/.wgea-mcp/cache.db` is corruption-self-healing on init
  (`sqlite3.DatabaseError` → unlink → recreate).
- Cache TTLs: `data` = 30 days (annual cadence), `catalog` / `landing` /
  `discovery` = 6 hours.

---

## The core 5-tool surface (uniform across sisters — mandatory)

The 5 below are the uniform brand. Additional tools (e.g. `top_n`, `stats`) are
allowed where the data shape genuinely needs them — they must use the same
`Annotated[Field]` discipline and `DataResponse` envelope as the core 5.

1. `search_datasets(query, limit)` — fuzzy search across the eight curated WGEA datasets
2. `describe_dataset(dataset_id)` — dimensions + measures + source URL + current reporting year
3. `get_data(dataset_id, filters, start_period, end_period, format, max_rows)` — query
4. `latest(dataset_id, filters, max_rows)` — latest reporting year only
5. `list_curated()` — enumerate supported IDs

Every parameter uses `Annotated[Type, Field(description=..., examples=[...])]`.
This is the Glama Tool Definition Quality requirement — non-negotiable.

## Trust contract (every DataResponse carries)

```
source             "Workplace Gender Equality Agency"
source_url         https://data.gov.au/data/dataset/wgea-dataset
download_url       actual ZIP URL used (post-discovery)
attribution        full CC-BY 3.0 AU attribution string with licence URL
retrieved_at       UTC timestamp
server_version     importlib.metadata.version("wgea-mcp")
reporting_year     latest reporting year present in the response
did_you_mean       fuzzy-match hints when an employer-name filter didn't resolve exactly
stale              True when serving cached fallback after upstream error / seed-manifest fallback
stale_reason       human-readable when stale=True
truncated_at       int | None — set when max_rows capped the post-filter result
```

## The 5 quality dimensions (audit every release against these)

1. **Semantic Clarity** — verb-noun tool names, Annotated[Field] with examples, rich docstrings (Examples + When to use + Returns blocks), `pattern=` constraints where IDs have known shapes
2. **Data Pruning** — <10k tokens for typical responses, `latest()` and `max_rows` cap large register dumps, fuzzy-matched employer hints surface in `did_you_mean` rather than padding `records`
3. **Cross-Agency Joining** — uniform period format conventions (YYYY-YY for WGEA, YYYY / YYYY-MM / YYYY-Q1 / YYYY-S1 / YYYY-MM-DD elsewhere); standardise on ABN / ANZSIC where the data supports it (WGEA's per-employer rows carry ABN)
4. **Reliability + Caching** — SQLite cache TTLs (30 days for annual data, 6h for CKAN catalog), self-heal on `sqlite3.DatabaseError`, **graceful degradation**: when data.gov.au is unreachable, fall back to the last cached ZIP and set `stale=True, stale_reason="..."` rather than raising
5. **Deterministic Error Handling** — every `ValueError` carries a "Try X" / "Did you mean X?" / "Valid options: ..." hint that suggests the correction, not just describes the rejection

## Test taxonomy

Required: `test_cache.py`, `test_curated.py`, `test_server_validation.py`, `test_shaping.py`, `test_client.py`, `test_models.py`, `test_catalog.py`, `test_discovery.py`, `test_parsing.py`, `test_customer_flows.py`, `test_did_you_mean.py`, `test_bug_regression.py`, `test_live.py` (live, `@pytest.mark.live`)

Zero-flake bar: full unit suite must run 10× consecutively green before tagging a release.

## Release workflow (Trusted Publishing via OIDC, no API tokens in CI)

```
1. Bump version in pyproject.toml (semver)
2. Update CHANGELOG.md (latest entry at top, semver headings)
3. uv run pytest × 10 — zero flakes
4. git commit -am "X.Y.Z: <one-line reason>"
5. git tag -a vX.Y.Z -m "X.Y.Z: <reason>"
6. git push origin main vX.Y.Z
7. Create a GitHub Release for the tag — publish.yml fires → builds → OIDC publish → PyPI
```

The workflow trigger is `release: published` (NOT `push: tag`), so tags
alone don't publish — you must create a GitHub Release. The pending-publisher
on PyPI is bound to workflow `publish.yml`.

## Anti-patterns — DO NOT do these

- Don't rename `publish.yml` to `release.yml`. PyPI Trusted Publishing's
  pending-publisher is configured against the literal filename `publish.yml`;
  the rename would break OIDC auth on the next release. The sister repos
  use `release.yml` — wgea-mcp is the documented exception.
- Don't add tools that duplicate or rename the core 5; their names/shapes are fixed. Extras are allowed only where the data shape genuinely needs them (e.g. `top_n`, `stats`) and must follow the same `Annotated[Field]` + `DataResponse` discipline
- Don't add new top-level dependencies beyond what other sisters use (httpx, pydantic, fastmcp, aiosqlite, rapidfuzz, pyyaml, openpyxl, pandas, aus-identity)
- Don't bundle large XLSX/CSV fixtures in the wheel; cache at runtime. The bundled `tests/fixtures/wgea_sample.zip` is intentionally truncated to <50 KB.
- Don't ship without 10 consecutive zero-flake pytest runs
- Don't echo PyPI tokens / PATs in tool output, commit messages, or CHANGELOG
- Don't classify a slow source API (>2s cold) as a bug; only flag >10s or actual errors
- Don't widen scope mid-audit-loop; loops are fix-only

## Common operations

```bash
cd .                                                       # in the repo
uv sync --extra dev                                        # install deps
uv run pytest                                              # unit tests
uv run pytest -m live                                      # live tests too (downloads ~71 MB)
uvx --refresh --from wgea-mcp==<ver> python -c "..."        # smoke a published wheel
```
