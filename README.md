# wgea-mcp

[![PyPI](https://img.shields.io/pypi/v/wgea-mcp.svg)](https://pypi.org/project/wgea-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/wgea-mcp.svg)](https://pypi.org/project/wgea-mcp/)
[![License](https://img.shields.io/pypi/l/wgea-mcp.svg)](https://github.com/Bigred97/wgea-mcp/blob/main/LICENSE)
[![Tests](https://github.com/Bigred97/wgea-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Bigred97/wgea-mcp/actions/workflows/test.yml)
[![CodeQL](https://github.com/Bigred97/wgea-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/Bigred97/wgea-mcp/actions/workflows/codeql.yml)
[![Glama MCP server quality](https://glama.ai/mcp/servers/Bigred97/wgea-mcp/badges/score.svg)](https://glama.ai/mcp/servers/Bigred97/wgea-mcp)

**MCP server for the Workplace Gender Equality Agency (WGEA) public data file.** Plain-English access to per-employer workforce composition, gender-equality policy answers, parental leave, flexible work, and harm-prevention data — every WGEA-reporting employer in Australia (~9,600 employers), every year, from a single `uvx` command.

```text
"What's the gender breakdown at Commonwealth Bank?"
"Which mining companies set gender targets in 2024-25?"
"Workforce composition by occupation at Qantas"
"Sexual harassment policy responses across financial services"
"Promotions to manager by gender at Atlassian"
```

Sister to [abs-mcp](https://github.com/Bigred97/abs-mcp), [rba-mcp](https://github.com/Bigred97/rba-mcp), [ato-mcp](https://github.com/Bigred97/ato-mcp), [apra-mcp](https://github.com/Bigred97/apra-mcp), [aihw-mcp](https://github.com/Bigred97/aihw-mcp), [asic-mcp](https://github.com/Bigred97/asic-mcp), and [au-weather-mcp](https://github.com/Bigred97/au-weather-mcp).

---

## Install

```bash
uvx --upgrade wgea-mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "wgea": { "command": "uvx", "args": ["--upgrade", "wgea-mcp"] }
  }
}
```

### Claude Code

```bash
claude mcp add wgea --command uvx --args -- --upgrade wgea-mcp
```

---

## What it exposes

Six tools, all plain-English in, structured out:

| Tool                | Purpose                                                       |
|---------------------|---------------------------------------------------------------|
| `search_datasets`   | Fuzzy-search the curated catalog by keyword                   |
| `describe_dataset`  | List a dataset's filterable dimensions and returnable measures |
| `get_data`          | Query with `filters`, period range, output format             |
| `latest`            | Restrict to the latest reporting year. Row cap parameter is `limit` (portfolio-standard); `max_rows` retained as legacy alias. |
| `top_n`             | Rank rows by a numeric measure (e.g. `n_employees`) and return the top (or bottom) N for one reporting year. |
| `list_curated`      | Enumerate the curated dataset IDs                             |

Every response is the same shape — `dataset_id`, `dataset_name`, `query`, `reporting_year`, `unit`, `row_count`, `records`, `source_url`, `download_url`, `did_you_mean`, `attribution`, `stale` flag, `server_version`.

---

## Curated datasets (8)

| ID                          | What it is                                                                  | Source |
|-----------------------------|-----------------------------------------------------------------------------|--------|
| `HEADLINE_GAP`              | Industry gender pay gap mid-points by ANZSIC division + national rollup     | EGPG xlsx on `wgea.gov.au` (aggregated server-side) |
| `WORKFORCE_COMPOSITION`     | Per-employer headcount by occupation × manager category × gender            | `wgea_workforce_composition_<year>.csv` |
| `WORKFORCE_MANAGEMENT`      | Manager movements (promotions, hires, resignations) by gender               | `wgea_workforce_management_statistics_<year>.csv` |
| `GENDER_EQUALITY_ACTIONS`   | Pay-gap analyses, gender targets, governance — Q&A responses                | `wgea_questionnaire_action_on_gender_equality_<year>.csv` |
| `PARENTAL_LEAVE_FLEX`       | Parental leave + flexible-work policy responses                             | `wgea_questionnaire_flexible_work_<year>.csv` |
| `HARM_PREVENTION`           | Sexual harassment + domestic-violence policy responses                       | `wgea_questionnaire_harm_prevention_<year>.csv` |
| `EMPLOYEE_SUPPORT`          | Carer leave, EAP, mental-health programs                                     | `wgea_questionnaire_employee_support_<year>.csv` |
| `WORKPLACE_OVERVIEW`        | Board composition, governing-body diversity, CEO + KMP demographics          | `wgea_questionnaire_workplace_overview_<year>.csv` |

> **The headline gender pay-gap %.** `HEADLINE_GAP` (added in 0.6.0) carries the industry mid-points WGEA publishes annually in its Employer Gender Pay Gaps Report — e.g. Mining 18.9%, Financial and Insurance Services 21.4%, "All employers" national mid-point 11.2% (2024-25 reporting year). Numbers match WGEA's published Figure 4 to within rounding. For the workforce-weighted national aggregate (~21.1% private sector) WGEA states in its public materials, see the [WGEA Data Explorer](https://www.wgea.gov.au/Data-Explorer) — that figure requires payroll data WGEA holds before public release and is not derivable from the published employer-level xlsx.

---

## Reliability — 2-tier URL resolution

WGEA publishes the public data file annually under a single CKAN package on data.gov.au. Each annual release gets a fresh resource UUID:

1. **Live CKAN** — `package_show?id=wgea-dataset` returns every resource; the newest "WGEA Data — Public Data File" wins. Cached 6h.
2. **Bundled seed manifest** — when CKAN is unreachable, fall back to `data/seed_urls.json` shipped in the wheel. The response is flagged `stale: true` with an honest reason.

Net effect: a fresh `uvx wgea-mcp` always gets the current reporting year; a 12-month-old install still works because the seed manifest is refreshed and `--upgrade` pulls a new wheel.

---

## Fuzzy employer-name search

Pass any abbreviation, alias, or substring and rapidfuzz resolves it:

| You type                              | Resolved to                                                  |
|---------------------------------------|--------------------------------------------------------------|
| `"CBA"`                               | Commonwealth Bank of Australia                               |
| `"Commonwealth Bank"`                 | Commonwealth Bank of Australia                               |
| `"NAB"`                               | National Australia Bank Limited                              |
| `"Westpac"`                           | Westpac Banking Corporation                                  |
| `"Woolies"` / `"woolworths"`          | Woolworths Group Limited                                     |
| `"Atlassian"`                         | Atlassian Pty Ltd                                            |
| `"qantas"`                            | Qantas Airways Limited                                       |

When nothing exact matches, `did_you_mean` carries the top-5 closest legal names so the agent can ask the user to pick.

---

## Attribution

Data sourced from the Workplace Gender Equality Agency. Licensed under [Creative Commons Attribution 3.0 Australia (CC BY 3.0 AU)](https://creativecommons.org/licenses/by/3.0/au/). wgea-mcp is MIT-licensed; WGEA's data carries the upstream CC-BY 3.0 AU licence, echoed in every response's `attribution` field.

Per-employer reporting is a deliberate disclosure under the Workplace Gender Equality Act 2012 — redistribution is explicitly intended.

---

## Sister MCPs (Australian Public Data portfolio)

- [abs-mcp](https://pypi.org/project/abs-mcp/) — Australian Bureau of Statistics (CPI, unemployment, ERP, building approvals)
- [rba-mcp](https://pypi.org/project/rba-mcp/) — Reserve Bank of Australia (cash rate, lending stats, exchange rates)
- [ato-mcp](https://pypi.org/project/ato-mcp/) — Australian Taxation Office (tax stats, ACNC charities)
- [apra-mcp](https://pypi.org/project/apra-mcp/) — Australian Prudential Regulation Authority (banking, insurance, super)
- [aihw-mcp](https://pypi.org/project/aihw-mcp/) — Australian Institute of Health and Welfare
- [asic-mcp](https://pypi.org/project/asic-mcp/) — Australian Securities and Investments Commission (company registers)
- [aemo-mcp](https://pypi.org/project/aemo-mcp/) — Australian Energy Market Operator (NEM dispatch, spot prices, generation)
- [au-weather-mcp](https://pypi.org/project/au-weather-mcp/) — Open-Meteo (Bureau of Meteorology aggregator)
- **wgea-mcp** — this one. Workplace gender equality.
- [aus-identity](https://pypi.org/project/aus-identity/) — Postcode / state / ABN normalisation helper used by all sisters

---

## Development

```bash
git clone https://github.com/Bigred97/wgea-mcp.git
cd wgea-mcp
uv venv
uv pip install -e ".[dev]"
pytest                  # unit tests
pytest -m live          # integration tests against data.gov.au (downloads the ~71 MB ZIP)
```

Issues and contributions welcome: [github.com/Bigred97/wgea-mcp/issues](https://github.com/Bigred97/wgea-mcp/issues).
