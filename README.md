# wgea-mcp

[![tests](https://github.com/Bigred97/wgea-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Bigred97/wgea-mcp/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/wgea-mcp.svg)](https://pypi.org/project/wgea-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/wgea-mcp.svg)](https://pypi.org/project/wgea-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
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

Five tools, all plain-English in, structured out:

| Tool                | Purpose                                                       |
|---------------------|---------------------------------------------------------------|
| `search_datasets`   | Fuzzy-search the curated catalog by keyword                   |
| `describe_dataset`  | List a dataset's filterable dimensions and returnable measures |
| `get_data`          | Query with `filters`, period range, output format             |
| `latest`            | Restrict to the latest reporting year                         |
| `list_curated`      | Enumerate the curated dataset IDs                             |

Every response is the same shape — `dataset_id`, `dataset_name`, `query`, `reporting_year`, `unit`, `row_count`, `records`, `source_url`, `download_url`, `did_you_mean`, `attribution`, `stale` flag, `server_version`.

---

## Curated datasets (7 in v0.1)

| ID                          | What it is                                                                  | Source CSV |
|-----------------------------|-----------------------------------------------------------------------------|------------|
| `WORKFORCE_COMPOSITION`     | Per-employer headcount by occupation × manager category × gender            | `wgea_workforce_composition_<year>.csv` |
| `WORKFORCE_MANAGEMENT`      | Manager movements (promotions, hires, resignations) by gender               | `wgea_workforce_management_statistics_<year>.csv` |
| `GENDER_EQUALITY_ACTIONS`   | Pay-gap analyses, gender targets, governance — Q&A responses                | `wgea_questionnaire_action_on_gender_equality_<year>.csv` |
| `PARENTAL_LEAVE_FLEX`       | Parental leave + flexible-work policy responses                             | `wgea_questionnaire_flexible_work_<year>.csv` |
| `HARM_PREVENTION`           | Sexual harassment + domestic-violence policy responses                       | `wgea_questionnaire_harm_prevention_<year>.csv` |
| `EMPLOYEE_SUPPORT`          | Carer leave, EAP, mental-health programs                                     | `wgea_questionnaire_employee_support_<year>.csv` |
| `WORKPLACE_OVERVIEW`        | Board composition, governing-body diversity, CEO + KMP demographics          | `wgea_questionnaire_workplace_overview_<year>.csv` |

> **Note on the headline gender-pay-gap %.** WGEA's Data Explorer publishes a headline per-employer gender pay gap percentage. That specific aggregate is NOT included in the public CSV release — WGEA pre-aggregates remuneration data before public publication. Use this MCP for the underlying workforce composition + policy detail; use [WGEA's Data Explorer](https://www.wgea.gov.au/Data-Explorer) for the headline pay-gap percentage.

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

## Sister packages

- [abs-mcp](https://github.com/Bigred97/abs-mcp) — ABS census + economic statistics
- [rba-mcp](https://github.com/Bigred97/rba-mcp) — RBA F-tables (cash rate, FX rates, mortgage rates)
- [ato-mcp](https://github.com/Bigred97/ato-mcp) — ATO tax statistics + ACNC charities register
- [apra-mcp](https://github.com/Bigred97/apra-mcp) — banks, super funds, insurance
- [aihw-mcp](https://github.com/Bigred97/aihw-mcp) — health and welfare statistics
- [asic-mcp](https://github.com/Bigred97/asic-mcp) — corporate transparency + AFS licensees
- **wgea-mcp** — this one. Workplace gender equality.
- [au-weather-mcp](https://github.com/Bigred97/au-weather-mcp) — Australian weather

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
