# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-14

### Initial release

wgea-mcp v0.1.0 ships seven curated WGEA datasets across workforce
composition, manager movements, and questionnaire responses, exposed
through a five-tool MCP surface that mirrors abs-mcp / rba-mcp / ato-mcp /
apra-mcp / aihw-mcp / asic-mcp.

### Tools (5)

- `search_datasets(query, limit=10)` — fuzzy search the curated catalog
- `describe_dataset(dataset_id)` — list dimensions, measures, source
- `get_data(dataset_id, filters, start_period, end_period, format, max_rows)`
- `latest(dataset_id, filters, max_rows)` — restrict to the latest reporting year
- `list_curated()` — enumerate curated IDs

### Curated datasets (7)

- **`WORKFORCE_COMPOSITION`** — per-employer headcount by ANZSIC ×
  manager_category × occupation × gender. ~211k rows in the 2024-25
  release, ~9,600 distinct employers.
- **`WORKFORCE_MANAGEMENT`** — manager movements (promotions, hires,
  resignations) by gender × manager_type. ~235k rows.
- **`GENDER_EQUALITY_ACTIONS`** — Q&A responses to the WGEA
  questionnaire's "Action on gender equality" section (pay-gap analyses,
  gender targets, governance).
- **`PARENTAL_LEAVE_FLEX`** — parental leave + flexible work policy
  responses.
- **`HARM_PREVENTION`** — sexual harassment + domestic violence policy
  responses.
- **`EMPLOYEE_SUPPORT`** — carer leave, EAP, mental-health, wellbeing
  policy responses.
- **`WORKPLACE_OVERVIEW`** — board composition, governing-body diversity,
  CEO + KMP demographics.

### Reliability engineering

- **2-tier URL discovery** — data.gov.au CKAN `package_show` finds the
  newest "WGEA Data - Public Data File" resource at query time. When CKAN
  is unreachable, a bundled `data/seed_urls.json` manifest takes over and
  the response is flagged `stale: true` with an honest reason.
- **Schema-fingerprint warning surface** — `_apply_aliases` raises an
  actionable `ValueError` if any expected column disappears from the source
  CSV, with the first 6 columns it actually saw embedded in the message.
- **In-flight request dedup** — 50 parallel callers asking for the same
  ZIP fan in to exactly one HTTP request.
- **Host pinning** — `fetch_resource` refuses any URL outside `data.gov.au`
  (and rejects non-http(s) schemes). Defense-in-depth against scraper or
  seed-manifest corruption.
- **Cache self-heal** — corrupt `~/.wgea-mcp/cache.db` is detected on init
  and silently rebuilt.

### Fuzzy employer-name search

A static alias map for ~80 top Australian employers (CBA, NAB, Westpac,
Qantas, Woolworths, Atlassian, ...) resolves abbreviations to the WGEA
canonical legal name before rapidfuzz runs. WRatio ≥ 80 fuzzy fallback
catches misspellings. "Did you mean?" hints surface in the response's
`did_you_mean` field when nothing matched exactly.

### Trust contract

Every response includes:

- `source = "Workplace Gender Equality Agency"`
- `source_url` — canonical data.gov.au landing page
- `download_url` — the actual ZIP URL used (post-discovery)
- `attribution` — CC-BY 3.0 Australia string + license link
- `retrieved_at` — ISO UTC timestamp
- `reporting_year` — WGEA reporting year for the returned rows (e.g. "2024-25")
- `server_version` — wgea-mcp wheel version
- `stale` + `stale_reason` — true when CKAN failed and we served from
  the bundled seed
- `did_you_mean` — fuzzy-match suggestions when an employer-name filter
  didn't resolve exactly

### Quality bar

- 136 unit tests, 7 live integration tests against data.gov.au
- Zero-flake: full unit suite passes 10/10 sequential runs
- Fuzzy employer search 100% accuracy on the hand-crafted alias test set
  (CBA/NAB/Westpac/ANZ/Qantas/Woolies/Atlassian/Telstra and more)
- Schema fingerprint guards catch column renames
- Defensive validation guards on every MCP tool with "Try X" hints
