# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
