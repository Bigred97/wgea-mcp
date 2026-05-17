# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.7] - 2026-05-18

### Fixed — CI wheel-verify assertion (curated dataset count drift)

0.6.6's lint fix unblocked the lint step but the wheel-verify build
step was still failing — it asserted `n == 7` curated datasets but
`HEADLINE_GAP` was added in an earlier release, making the actual count
8. Changed to `n >= 7` so future curated additions don't re-break CI
the same way.

No runtime change vs 0.6.6.

235 unit tests pass.

## [0.6.6] - 2026-05-18

### Fixed — CI lint failure (E402 in tests/test_parsing.py)

0.6.5 release CI's test workflow failed lint because
`tests/test_parsing.py` uses a deliberate mid-file import below a
section comment block. Added `[tool.ruff]` config + `tests/*`
per-file-ignores list to pyproject.toml (matching the abs/apra/asic
pattern) so the intentional pattern stays valid.

No runtime change vs 0.6.5.

235 unit tests pass.

## [0.6.5] - 2026-05-18

### Fixed — `describe_dataset('HEADLINE_GAP')` 7-second cold call

Customer-sim reported `/v1/describe/wgea/HEADLINE_GAP` timing out. Root
cause: describe was eagerly calling `_fetch_and_parse_xlsx(cd)` to get
the current reporting year for the response's `reporting_year_latest`
field. On a cold-cache worker that fetch+parse took 6-7s, which combined
with gateway dispatch + JSON serialisation tripped the upstream timeout.

Wrapped the year-resolution call in `asyncio.wait_for(timeout=1.5)`.
Customers get `reporting_year_latest=None` when the underlying fetch
exceeds the cap (rare on warm cache, common on first cold request); the
year is correctly populated by the subsequent `get_data` call which is
the right place to pay the parse cost.

Describe cold-call now: **1.48s** (was 6.99s).

No data shape change; the field semantics already documented that
`reporting_year_latest` is best-effort. 235 tests pass.

## [0.6.4] - 2026-05-17

### Performance — Parquet on-disk parsed-DataFrame cache

The in-process LRU (`_df_cache`) handles warm queries in ~50ms but it's
empty on cold restart — the first call after a worker bounce paid the
full pandas/zipfile parse cost (13-22s for `EMPLOYEE_SUPPORT` at 154MB
/ 332k rows). The `ausdata-api` gateway's 20s timeout was tripping on
those cold parses.

Added a Parquet on-disk fallback in `parquet_cache.py`:

- After a parse, the DataFrame is persisted to
  `~/.wgea-mcp/parquet-cache/{sha256-of-cache-key}.parquet` (path
  overridable via `WGEA_MCP_PARQUET_CACHE_DIR` for tests + the Fly
  deploy's `/data` volume).
- Before parsing, check the file: read with `pd.read_parquet` if fresh
  (1-2s for the largest CSVs versus 13-22s pandas parse).
- TTL: 7 days, matching the SQLite byte-cache TTL for `data` kind.
  Use file mtime; expired files trigger a fresh parse.
- Self-heal: a corrupt Parquet file is unlinked, and the call falls
  through to a fresh parse + write — same pattern as the SQLite
  cache's corruption recovery.
- Writes go through a `.parquet.tmp` sibling + rename so a crash
  mid-write doesn't leave a corrupted file behind.

Cold-restart cost on `EMPLOYEE_SUPPORT`: 22s → 1.5s (about 14×).
Warm-cache (in-process LRU) still 50ms; this layer only kicks in after
a process restart.

### Internal

- Added `pyarrow>=15` dep (pandas Parquet engine).
- New `parquet_cache.py` module (~120 lines) + 7 regression tests in
  `tests/test_parquet_cache.py`.
- Wired into both `_fetch_and_parse` (ZIP/CSV path for the 7
  questionnaire datasets) and `_fetch_and_parse_xlsx_aggregated`
  (`HEADLINE_GAP`). Both paths now: in-memory cache hit → Parquet
  cache hit → fresh parse + write.

## [0.6.3] - 2026-05-17

### Improved — transport-agnostic Field descriptions

Four `Field(description=...)` strings in `server.py` referenced
MCP-tool-name (`search_datasets()`, `list_curated()`,
`describe_dataset()`). These descriptions become part of the parameter
schema, so REST-gateway customers calling `/v1/...` saw "Use
search_datasets() to discover" — confusing because they're not calling
a Python function. Rewrote to the "{endpoint or tool}" form. Matches
the ato 0.8.7 / rba 0.7.5 portfolio guard. No runtime behaviour change.

## [0.6.2] - 2026-05-17

### Fixed — CI wheel-verify hard-coded `n == 7` curated-count assert

`publish.yml`'s smoke-test step asserted exactly 7 curated datasets;
0.6.0 added `HEADLINE_GAP` bringing the total to 8, so the verify
step failed and the publish step didn't fire (both 0.6.0 and 0.6.1
hit this). Relaxed the assert to `>= 7` so future curated additions
don't break the publish path. The smoke-test's purpose is "wheel
installs and `list_curated` runs", not "exact dataset count" — that's
already covered by `test_curated.py` in the unit suite. No runtime
behaviour change vs 0.6.0 / 0.6.1.

## [0.6.1] - 2026-05-17

### Fixed — CI wheel-verify failed on local-directory `aus-identity` source

`pyproject.toml`'s `[tool.uv.sources]` block pointed `aus-identity` at the
sibling repo path `../aus-identity` (intended as a dev-loop convenience
while v0.3.0 was queued for PyPI). The 0.6.0 release workflow tried to
verify the wheel installs cleanly on CI runners, where that sibling path
doesn't exist, so the verification step failed and the publish step
didn't fire. Removed the override now that `aus-identity==0.3.0` is
live on PyPI; regenerated `uv.lock` to source from the registry. No
runtime behaviour change vs 0.6.0. 228 tests still pass.

## [0.6.0] - 2026-05-17

### Added — HEADLINE_GAP curated dataset (the most-searched WGEA number)

New 8th curated dataset `HEADLINE_GAP` answers the most-searched WGEA
questions — "what's Australia's gender pay gap?" and "what's the pay gap
in mining/finance/construction?" — which the existing 7 per-employer
datasets cannot. WGEA aggregates remuneration data BEFORE publishing the
per-employer CSVs on data.gov.au, so the rolled-up industry pay-gap
percentage lives only in WGEA's annually-published Employer Gender Pay
Gaps spreadsheet on wgea.gov.au.

- One row per ANZSIC division (19 divisions) plus a synthetic
  `All employers` national row. Mid-points match WGEA's published
  Figure 4 in the annual report (Construction 23.8%, Financial and
  Insurance Services 21.4%, Mining 18.9% in 2024-25).
- Five measures: `total_remuneration_gap_pct`, `base_salary_gap_pct`,
  `median_total_rem_gap_pct`, `median_base_salary_gap_pct`,
  `employer_count`. Percentages are out of 100.
- Filter `anzsic_division` accepts the full division name ('Mining'),
  the ANZSIC division letter ('B'), a 2/3/4-digit code ('06', '0801'),
  or a synonym ('mining', 'finance', 'banking', 'realestate'). Uses
  `aus-identity>=0.3.0` for cross-source canonical normalisation.
- Filter `anzsic_division=all` (or `national` / `australia` / `total`)
  returns the single national mid-point row.
- Source: `https://www.wgea.gov.au/sites/default/files/documents/Employer-Gender-Pay-Gaps-Spreadsheet.xlsx`
  (~2 MB xlsx, ~20 row response after server-side aggregation).
- Cached at runtime (`cache_kind: data`, 30-day TTL); no xlsx bytes
  bundled in the wheel. Falls back to last-good cached payload when
  upstream is unreachable (graceful degradation per portfolio dim #4).

### Added — wgea.gov.au allowlisted as a fetch host

`WGEAClient.fetch_resource` now accepts URLs on `wgea.gov.au` in addition
to `data.gov.au`. WGEA hosts the EGPG spreadsheet on its own site;
data.gov.au only carries the per-employer Public Data File ZIP.

### Added — `xlsx_aggregated` curated format

`curated.py` now accepts a second `format` value alongside `csv_in_zip`.
xlsx_aggregated datasets declare a stable `download_url` instead of
relying on CKAN discovery, and use `zip_member` to name the source sheet
within the spreadsheet. Loader rejects xlsx_aggregated YAMLs missing a
`download_url`.

### Added — ANZSIC division normaliser in `translate_filter_value`

The `anzsic_division` dimension now routes through `aus_identity` (v0.3+)
so users can pass any of: full division name, division letter (A-S),
2/3/4-digit ANZSIC code, or YAML alias. Backwards compatible — the
existing 7 datasets that don't expose this dimension are unaffected.

### Internal

- Added `openpyxl>=3.1` and `aus-identity>=0.3.0` to project deps.
- Added `parse_egpg_xlsx` to `parsing.py` (private-sector filter +
  industry groupby + percent conversion).
- Added `_fetch_and_parse_xlsx` to `server.py` dispatcher; HEADLINE_GAP
  routes through it (bypasses CKAN discovery + the streaming fast path,
  neither of which applies to the small aggregated response).
- Added tests/fixtures/wgea_egpg_sample.xlsx (~6 KB, 9 private + 1
  Commonwealth row across 3 industries) for offline parser tests.

## [0.5.4] - 2026-05-17

### Fixed — event-loop blocking on sync ZIP-CSV parse

`_fetch_and_parse` called `read_csv_from_zip` synchronously inside an
async tool body. WGEA's annual ZIP is ~71MB containing 7 thematic CSVs
(largest unzips to ~160MB); the sync parse blocked the event loop for
seconds, serialised concurrent requests behind one parse, and stalled
downstream consumers like the `ausdata-api` gateway against its 20s
budget. Wrapped in `asyncio.to_thread` so the parse runs on the default
executor without blocking other in-flight tool calls. Matches the
0.4.7 / 0.6.4 / 0.8.6 / 0.8.6 fixes in `aihw-mcp` / `asic-mcp` /
`apra-mcp` / `ato-mcp`.

## [0.5.3] - 2026-05-16

### Performance

- Extended the streaming/early-exit path to `GENDER_EQUALITY_ACTIONS` and
  `WORKFORCE_MANAGEMENT` (same fix as 0.5.2 for WORKFORCE_COMPOSITION and
  EMPLOYEE_SUPPORT). 4 of 7 wgea datasets now use the streaming path —
  the user's live LLM-workflow testing against `ausdata-api` was hitting
  timeouts on these two datasets at the gateway's 20s budget. Cold
  `limit=2` calls now short-circuit at parse time (rows_walked = max_rows + 1)
  instead of paying the full pandas-parse cost. The other 3 datasets
  (`PARENTAL_LEAVE_FLEX`, `HARM_PREVENTION`, `WORKPLACE_OVERVIEW`) keep
  the existing full-parse + parsed-DataFrame cache path — they're small
  enough that the warm-cache path was already meeting the budget.

### Added

- 2 regression tests in `test_bug_regression.py`:
  `test_gender_equality_actions_limit_short_circuits`,
  `test_workforce_management_limit_short_circuits`.
- Updated `test_streaming_other_datasets_unaffected` to assert the 3
  non-streaming datasets (was 5, now 3) still use the full-parse path.

201 unit tests now (was 199). 10x zero-flake green. Ruff clean.
No new dependencies. No envelope changes.

## [0.5.2] - 2026-05-16

### Performance — `WORKFORCE_COMPOSITION` + `EMPLOYEE_SUPPORT` lazy-stream fix

The two largest WGEA CSVs (56 MB / 154 MB uncompressed) were timing out
at the hosted API's 20s budget even for `limit=2` requests: the cold call
was paying the full ~5-13s pandas parse before any row truncation
happened. Fix: stream rows from the cached ZIP with `csv.reader` and
short-circuit at `max_rows + 1` (the `+1` keeps the truncation signal
intact so `DataResponse.truncated_at` still tells agents the cap was
hit). Filters that the streaming path can push down as simple equality
predicates (`gender=Women`, `anzsic_division=Mining`, `is_relevant_employer=true`,
etc.) are applied per-row at parse time. Fuzzy/wildcard filters
(`employer_name=CBA`, `subsection=Sexual*`) fall back to the existing
full-parse + cache path, so rapidfuzz and the alias map keep working
correctly.

Cold-call timings on the customer's blocking workload (`limit=2`):
- WORKFORCE_COMPOSITION: 5-12s → 0.1-0.3s
- EMPLOYEE_SUPPORT: 13-17s → 0.1-0.3s

The other 5 WGEA datasets (`WORKFORCE_MANAGEMENT`, `GENDER_EQUALITY_ACTIONS`,
`PARENTAL_LEAVE_FLEX`, `HARM_PREVENTION`, `WORKPLACE_OVERVIEW`) are
unaffected — they keep the existing full-parse + parsed-DataFrame cache
path which is already correct and fast on warm calls.

### Added

- `parsing.stream_csv_from_zip(zip_bytes, member_pattern, *, max_rows, row_predicate)`
  — stdlib-only streaming CSV reader from a ZIP member with early-exit.
- 4 regression tests in `test_bug_regression.py`:
  `test_workforce_composition_limit_short_circuits`,
  `test_employee_support_limit_short_circuits`,
  `test_streaming_other_datasets_unaffected`,
  `test_streaming_with_fuzzy_filter_falls_back_to_full_parse`.
- 6 unit tests in `test_parsing.py` covering the streaming function
  (short-circuit, predicate, max_rows guard, empty result, dtype inference).

199 unit tests now (was 189). 10x zero-flake green. Ruff clean.
No new dependencies. No envelope changes.

## [0.5.1] - 2026-05-16

### Fixed — JSON-string `filters` parameter (portfolio-wide)

The MCP protocol JSON-encodes dict parameters before they reach the
server. `_validate_filters` was checking `isinstance(filters, dict)`
before parsing the JSON string, so every call of the form
`get_data(filters={"employer_name":"Commonwealth Bank"})` from a real
MCP client was rejected. Fix: decode JSON-string filters before the
type check. Coordinated patch across the portfolio (abs 0.9.2, ato 0.8.2,
apra 0.8.2, asic 0.6.1, aihw 0.4.2, wgea 0.5.1, aemo 0.4.2).

## [0.5.0] - 2026-05-15

### Added

- **`limit` parameter on `latest()`** — additive, non-breaking alias for the
  legacy `max_rows`. Wave 4 of the portfolio interoperability pass:
  asic-mcp's `latest(..., limit)` is the portfolio-standard row-cap
  parameter for register-style data; wgea-mcp now accepts the same name so
  cross-sister calling patterns match. Same semantics, same default
  (`None` → 2000), same hard cap (10000). Supplying both `limit` and
  `max_rows` raises `ValueError` with a "Use either X or Y, not both"
  hint. `max_rows` continues to work unchanged.

  ```python
  # New canonical name (preferred)
  await latest("WORKFORCE_COMPOSITION", filters={"anzsic_division": "Mining"}, limit=100)

  # Legacy alias (still works)
  await latest("WORKFORCE_COMPOSITION", filters={"anzsic_division": "Mining"}, max_rows=100)
  ```

  `get_data(..., max_rows)` is intentionally LEFT UNCHANGED. It's a
  different surface (post-filter cap on a multi-year query, not the
  single-reporting-year register cap), and the audit recommendation was
  scoped to `latest()` only.

- **+4 regression tests** in `test_server_validation.py` locking in the
  alias contract: limit accepted, max_rows still works, both raises,
  neither still defaults.

- 190 unit tests now (was 186). 10x zero-flake green. Ruff clean.
- No new dependencies. No envelope changes.

## [0.4.0] - 2026-05-15

### Added

- **`top_n` tool** — rank rows by a numeric measure and return the top
  (or bottom) N. Wave 3 of the portfolio interoperability pass; signature
  matches aihw-mcp / apra-mcp / ato-mcp so an agent that learned `top_n`
  on one sister uses it identically here:

  ```python
  top_n(dataset_id, measure, n=10, filters=None,
        direction="top", reporting_year=None)
  ```

  WGEA adds an optional `reporting_year=` parameter — since the source data
  is annual, the rank should be scoped to a single year. Default is the
  latest reporting year (current behaviour of `latest()`); pass
  `reporting_year="2023-24"` to rank an earlier release.

  Common workflows:
  - "10 employers with the most women managers" →
    `top_n("WORKFORCE_COMPOSITION", "n_employees", n=10, filters={"gender": "Women", "manager_category": "Manager"})`
  - "5 ANZSIC divisions with the fewest Yes responses on Gender Pay Gap" →
    `top_n("GENDER_EQUALITY_ACTIONS", "n_responses", n=5, direction="bottom", filters={"section": "Gender Pay Gap", "response": "Yes"})`

## [0.3.0] - 2026-05-15

### Added

- **DataResponse.period**: canonical `{"start", "end"}` dict populated alongside
  the wgea-specific `reporting_year`. Cross-sister consumers can now read
  `resp.period["start"]` / `resp.period["end"]` uniformly across the portfolio.
  For a single-year response both bounds equal `reporting_year`; for multi-year
  spans they bracket the range. The legacy `reporting_year` field (the latest
  year in the response) is preserved unchanged.

## [0.2.0] — 2026-05-15

### Added — Wave 1 portfolio interoperability fixes

Cross-sister consistency pass on input handling + error messages. Three
additive, non-breaking changes that bring `wgea-mcp` up to the abs/aihw/ato
standard identified in the portfolio interoperability audit.

- **Int-year coercion in period validation.** `start_period=2024` (a bare
  JSON int) now coerces to `"2024"` instead of raising a TypeError-style
  message. LLM clients routinely send JSON ints; this removes a confusing
  failure mode that surfaced as `must be a string, got int`. Out-of-range
  ints (e.g. `12345`, `1800`) still raise — with a hint pointing at the
  canonical `'YYYY-YY'` / `'YYYY'` forms.
- **Strengthened `ValueError` messages.** Every rejection now follows the
  canonical shape `<rejection>. Did you mean X?. Valid options: <list>. Try
  <tool>(<args>) for more.`. New rapidfuzz-driven "Did you mean ...?"
  hints on: unknown dataset_ids (`'WORKFORCE_COMPOSTION'` →
  `'WORKFORCE_COMPOSITION'`), unknown `format` values, and unknown filter
  keys. Period-format reminders added to both invalid-format and
  end-before-start errors, with worked examples.
- **Type signature broadened** on `get_data`'s `start_period` /
  `end_period` to `str | int | None` so the tool's published schema
  reflects the new coercion behaviour.

7 new unit tests in `tests/test_server_validation.py` cover the coercion
boundary, the `bool`-subclass-of-int guard, the dataset-id suggestion
hint, the strengthened period-format/swap errors, and the existing
non-string test was updated to match the new shape.

### Backward compatibility

No breaking changes. Inputs that previously raised a type error on bare
int years now succeed; every existing rejection path still raises, just
with a more actionable message.

## [0.1.5] — 2026-05-15

### Bug fix — Glama Tool Definition Quality compliance

`latest()`'s `max_rows` parameter had `description=` but was missing the
`examples=[...]` Field annotation. CLAUDE.md flags this as a
**non-negotiable Glama Tool Definition Quality requirement**. Caught
during an end-to-end pass that enumerated every tool's parameter schema
and counted missing examples — 1/16 parameters non-compliant.

Fix: added `examples=[100, 500, 2000]` to `latest()`'s `max_rows`. All
five tools now meet the Glama bar — every parameter has both
`description` and `examples`.

### Tests

- 2 new regression tests under `test_bug_regression.py::test_bug7_*`:
  - `test_bug7_all_tool_params_have_examples` — enumerate every tool's
    parameters; assert all have `examples`
  - `test_bug7_all_tool_params_have_descriptions` — same for `description`
- Full unit suite: **162 tests passing** (was 160)
- Zero-flake 10/10

## [0.1.4] — 2026-05-15

### Bug fix — `truncated_at` field was never set

The `DataResponse.truncated_at` field was added in 0.1.3 and documented in
the CLAUDE.md trust contract ("set when max_rows capped the post-filter
result"), but the shaping code never populated it. Agents asking for
e.g. `max_rows=10` over a result of thousands had no signal that they
were seeing a truncated view.

Fix: `build_response` now records the pre-truncation row count and sets
`truncated_at = pre_count` when `max_rows` clips the result. None when
the result fits under the cap or when `max_rows` was not specified.

### Tests

- 3 new regression tests under `test_bug_regression.py::test_bug6_*`:
  - `test_bug6_truncated_at_set_when_capped`
  - `test_bug6_truncated_at_none_when_under_cap`
  - `test_bug6_truncated_at_none_when_max_rows_none`
- Full unit suite: **160 tests passing** (was 157)
- Zero-flake 10/10
- 7 live tests against data.gov.au still green

## [0.1.3] — 2026-05-15

### Graceful degradation — fall back to stale cache on upstream failure + CLAUDE.md for sister-MCP parity

- **`WGEAClient` now falls back to the last cached ZIP when data.gov.au is
  unreachable.** Previously a 5xx from data.gov.au or an `httpx.ConnectError`
  bubbled up as `WGEAAPIError` and broke the calling agent's chain of
  reasoning. Now the client serves the cached payload (regardless of TTL),
  records the reason in a per-context `_stale_signal` ContextVar, and
  `_get_data_impl` copies it onto `DataResponse.stale / stale_reason`.
  Original raise-behaviour is preserved when there's no cache to fall
  back to — fail-closed when there's nothing safe to serve.
- **`Cache.get_stale(key)`** — new lookup that returns
  `(payload, cached_at_epoch)` regardless of TTL. The building block for
  the graceful-degradation path.
- **`DataResponse.truncated_at: int | None`** added (matches the
  portfolio-wide envelope; previously absent from wgea-mcp).
- **`CLAUDE.md`** authored at repo root for parity with abs-mcp / rba-mcp /
  ato-mcp / apra-mcp / aihw-mcp / asic-mcp. Documents the source agency,
  curated dataset list, the 5-tool surface, the trust contract, and the
  repo-specific gotcha that the publish workflow is named `publish.yml`
  (not `release.yml` like the sisters) — PyPI Trusted Publishing's
  pending-publisher is bound to that filename.

### Tests

- 4 new regression tests under `test_client.py` pin the stale-fallback
  behaviour: 5xx → fallback, ConnectError → fallback, empty-cache → raises,
  `Cache.get_stale()` round-trip.
- Full unit suite: 158 tests passing (was 154).
- Zero-flake on 3 consecutive runs.

## [0.1.2] — 2026-05-15

### Bug fix

- **Fuzzy ranking of "Commonweath Bank" (and similar typos) picked
  "Bendigo And Adelaide Bank Limited" over "Commonwealth Bank Of
  Australia".** Root cause: rapidfuzz WRatio ties both at 85.5 because
  they share "Bank" and similar length, and the tie-breaking order is
  effectively non-deterministic. Same problem for `Comonwelth Bank`,
  `Cwlth Bank`, and `Natoinal Australia`. Fix: blend WRatio with
  partial_ratio (the substring scorer), which decisively favours
  Commonwealth (partial_ratio 93.75 vs 50). Threshold lowered from 80
  to 75 to keep accepting clear matches. Verified on 7 hand-crafted
  typos: 7/7 correct.

### Tests

- 9 new regression tests under `test_bug_regression.py::test_bug5_*`
- Full unit suite: 154 tests passing (was 145)
- Zero-flake 10/10

## [0.1.1] — 2026-05-15

### Bug fixes (real customer impact)

- **Multi-employer alias filter returned 0 rows.** Passing a list of
  abbreviations like `{"employer_name": ["CBA", "NAB", "Westpac", "ANZ"]}`
  hit the list-of-values branch, which translated each entry through
  `translate_filter_value` (no-op for `employer_name`) instead of running
  it through the alias map + fuzzy matcher. Each abbreviation stayed
  un-expanded so the `isin()` mask matched nothing. Fix: route every list
  entry through `fuzzy_match_employer` when the column is in the fuzzy
  set (`employer_name`, `corporate_group_name`).
- **`max_rows` negative / zero / too-large / bool silently fell back to
  the default cap of 2000.** A FastMCP `Field` constraint catches these
  when invoked through the protocol, but direct programmatic calls (and
  some MCP clients that don't honour `ge`/`le` on Annotated metadata)
  could slip through. Fix: explicit validation in `_get_data_impl`
  rejecting non-positive, non-int, and >10_000 values.
- **`None` filter value matched spurious rows via fuzzy.** Passing
  `{"employer_name": None}` was `str()`-coerced to `"None"` and then
  fuzzy-matched against `"Noni B Limited"` (and similar N-prefix names)
  with score ≥80. Fix: reject `None` and `None`-in-list filter values
  up-front with an actionable error.
- **50 parallel callers hit the same URL → 2 HTTP requests** (instead
  of the expected 1). Root cause: SQLite snapshot isolation. Late
  callers whose `cache.get` connection opened *before* the writer's
  commit saw a pre-write snapshot, returned MISS, and started fresh
  HTTP requests. Fix: an in-memory LRU of the most recent 16 fetch
  results, consulted right after the SQLite `cache.get` miss and
  before the in-flight check. With the patch, 10/10 trials of 50
  parallel callers fan in to exactly 1 HTTP request.

### Tests

- 145 unit tests (was 136 — 9 new regression tests pin each fix)
- 7 live tests (unchanged)
- Zero-flake 10/10 sequential runs

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
