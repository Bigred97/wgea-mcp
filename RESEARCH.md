# wgea-mcp — Phase 1 Research

**Date:** 2026-05-14
**Author:** Harry Vass
**Status:** ⏸️ PAUSE for review before Phase 2 build

---

## TL;DR — Feasibility Verdict

| Question                       | Verdict                       | Confidence |
| ------------------------------ | ----------------------------- | ---------- |
| Is the backend reliable?       | ✅ **YES** — CKAN auto-discovery | High       |
| Can it be updated easily?      | ✅ **YES** — annual, schema-stable since 2021 | High |
| Does it add value?             | ✅ **YES** — strong "ask by employer name" use case | High |
| Will customers pay for it?     | 🟡 **YES, niche** — smaller TAM than ATO/APRA, higher per-customer willingness to pay | Medium-High |
| Build effort vs brief's 2-3 day estimate | ✅ Confirmed — 2.5 days realistic for v0.1.0 | High |

**Recommendation: BUILD.** This is the cleanest CKAN backend of any sibling so far — single package, 49 resources, CC-BY 3.0 AU licence, schema is tidy long-format CSV. The 2025 release (Jan 2026) is already structured as 8 thematic CSVs that map almost 1:1 to the brief's proposed 5-7 datasets, which means very little curation work is needed.

---

## 1a. Backend Inventory

### Selected backend: data.gov.au CKAN (single package)

| Property         | Value                                                           |
| ---------------- | --------------------------------------------------------------- |
| CKAN endpoint    | `https://data.gov.au/data/api/3/action/package_show?id=wgea-dataset` |
| Package UUID     | `4d35cd80-2538-4705-82f3-d0d18e823d98`                          |
| Title            | WGEA Dataset                                                    |
| Organisation     | Workplace Gender Equality Agency                                |
| Total resources  | **49**                                                          |
| Temporal coverage | **2014-04-01 → 2025-03-31** (12 reporting years)                |
| Licence          | **Creative Commons Attribution 3.0 Australia** (`cc-by`)        |
| Licence URL      | `http://creativecommons.org/licenses/by/3.0/au/`                |
| Metadata last modified | `2026-01-09T11:03:34Z`                                    |
| Auth required    | None — fully public, no API key                                 |
| Rate limit       | None (standard CKAN public endpoint)                            |

### Alternatives considered and rejected

| Source | Why rejected |
| --- | --- |
| `https://www.wgea.gov.au/data-statistics` | Marketing page, no bulk downloads, requires HTML scraping |
| `https://www.wgea.gov.au/Data-Explorer` | Interactive dashboard only, no API, blocked behind JS |
| `data.wgea.gov.au` | 302-redirects to the Data Explorer dashboard; not a data portal |
| Manual scraping of WGEA report PDFs | Brittle; the same data is in CKAN as clean CSV |

**Conclusion:** CKAN is unambiguously the right backend. Same pattern as **asic-mcp** (CKAN auto-discovery, multi-resource package). The wgea-dataset package is even cleaner — a single package with 49 well-named resources, versus asic-mcp's seven separate packages.

### Backend reliability signal
- Package is **continuously updated** — most recent edit `2026-01-09` (today is 2026-05-14, so ~4 months stale, which is normal between annual releases)
- 12 consecutive years of releases — no gaps
- WGEA has a **legal mandate** under the Workplace Gender Equality Act 2012 to publish this data annually
- ✅ Highest confidence backend of any sibling project to date

---

## 1b. The 2025 Public Data File (latest, posted Jan 2026)

The 2025 release is a single 71 MB ZIP that unpacks to **8 thematic CSVs** totalling ~952 MB uncompressed:

| File                                              | Size (MB) | Rows    | What it is |
| ------------------------------------------------- | --------: | ------: | ---------- |
| `wgea_workforce_composition_2025.csv`             |        56 | 211,660 | **Employer × occupation × manager category × gender headcount** — the killer dataset |
| `wgea_workforce_management_statistics_2025.csv`   |        66 | 235,784 | **Pay statistics by manager category × gender** — pay gap data lives here |
| `wgea_questionnaire_workplace_overview_2025.csv`  |       102 |       — | Organisation profile answers |
| `wgea_questionnaire_action_on_gender_equality_2025.csv` |   73 |       — | Policy + strategy responses |
| `wgea_questionnaire_employee_support_2025.csv`    |       154 |       — | Family/domestic violence, carer support |
| `wgea_questionnaire_flexible_work_2025.csv`       |       113 |       — | Parental leave + flexible work answers |
| `wgea_questionnaire_harm_prevention_2025.csv`     |       342 |       — | Sexual harassment policy responses (largest file) |
| `wgea_questionnaire_catalogue_2025.csv`           |     0.19  |     540 | Question dictionary — `question_index` → `question_text` map |

### Confirmed column schema — `wgea_workforce_composition_2025.csv` (headers verified)

```
reporting_year, corporate_group_name, employer_name, employer_abn,
is_relevant_employer, employer_size, anzsic_code, anzsic_division,
anzsic_subdivision, anzsic_group, anzsic_class,
manager_category, occupation, employment_status, employment_type,
gender, n_employees
```

**Sample row:**
```
2024-25, 1-STOP CONNECTIONS PTY LIMITED, 1-STOP CONNECTIONS PTY LIMITED,
58102573544, TRUE, <250, 7000, "Professional, Scientific and Technical Services",
Computer System Design and Related Services, ..., Manager, CEOs,
Full-time, Permanent, Women, 1
```

This is **long-format ("tidy")** — every (employer × ANZSIC × manager_category × occupation × employment_status × employment_type × gender) combination is one row with `n_employees`. Trivial to filter and aggregate.

### Coverage
- **~8,500 employers + 1,600 corporate groups** (first year to include private + Commonwealth public sector combined)
- All employers with 100+ employees (legal threshold under WGE Act 2012)
- 12 years of history available (older years in XLSX, 2021+ in CSV)

---

## 1b-ii. Proposed v0.1.0 curated datasets (6 datasets — clean 1:1 mapping)

| dataset_id              | Source CSV(s)                                                    | Description |
| ----------------------- | ---------------------------------------------------------------- | ----------- |
| `workforce_composition` | `wgea_workforce_composition_2025.csv`                            | Employer × occupation × gender headcount — the **employer_name fuzzy search** anchor |
| `pay_gap`               | `wgea_workforce_management_statistics_2025.csv`                  | Manager-level pay statistics by gender; computed gender pay gap |
| `industry_pay_gap`      | Derived aggregate from `workforce_management_statistics` over `anzsic_division` | Industry-average pay gap (Top 10 / Bottom 10 lists) |
| `parental_leave`        | `wgea_questionnaire_flexible_work_2025.csv`                      | Parental leave + flexible work policies |
| `harm_prevention`       | `wgea_questionnaire_harm_prevention_2025.csv`                    | Sexual harassment policy responses |
| `gender_equality_actions` | `wgea_questionnaire_action_on_gender_equality_2025.csv`        | Strategy + policy actions by employer |

**Rationale for 6 not 7:** the catalogue CSV is metadata (question dictionary), not a curated dataset — it gets exposed via `describe_dataset()` instead. `employee_support` and `workplace_overview` are deferred to v0.2.0 as power-user datasets.

**Killer filter on every dataset:** `employer_name` (free-text, fuzzy via rapidfuzz, like ato-mcp's charity name search). Users can ask:

- "What's the pay gap at Commonwealth Bank?" → fuzzy-match `Commonwealth Bank of Australia`
- "Top 10 worst gender pay gaps in mining" → filter `anzsic_division=Mining`, sort by computed gap desc
- "Industry-average pay gap for retail" → aggregate over `anzsic_division=Retail Trade`
- "Companies that improved most since last year" → time-series compare `2024-25` vs `2023-24`
- "Pay gap by occupation: senior managers" → filter `manager_category=Senior Manager`

All five demo prompts from the brief are answerable with these 6 datasets.

---

## 1c. Licence + Attribution

| Item            | Value                                                                |
| --------------- | -------------------------------------------------------------------- |
| Licence         | **Creative Commons Attribution 3.0 Australia** (CC-BY 3.0 AU)        |
| Licence URL     | `http://creativecommons.org/licenses/by/3.0/au/`                     |
| Confirmed by    | CKAN `package_show` response (`license_id: cc-by`)                   |
| Same as         | **apra-mcp** (also CC-BY 3.0 AU) — paste the exact attribution block |

### Attribution string (mandatory in every DataResponse)

```
Source: Workplace Gender Equality Agency. Licensed under CC-BY 3.0 Australia
(https://creativecommons.org/licenses/by/3.0/au/). Original dataset:
https://data.gov.au/data/dataset/wgea-dataset
```

### Redistribution
- ✅ Per-employer data is **published deliberately** under the Workplace Gender Equality Act 2012 — Parliament mandated public disclosure
- ✅ No personal information; all aggregated at employer level (100+ employees)
- ✅ Cleanest licence case of any sibling — even cleaner than ASIC (where company name search has reasonable-use considerations)

---

## 1d. Annual Cadence

### Confirmed release dates from CKAN metadata
| Reporting year | Released         | Months after period end |
| -------------- | ---------------- | ----------------------- |
| 2024-25        | 2026-01-09       | ~9 months               |
| 2023-24        | 2025-07-30       | ~4 months               |
| 2022-23        | 2024-01-07       | ~9 months               |
| 2021-22        | 2022-12-11       | ~8 months               |
| 2020-21        | 2022-05-02       | ~13 months              |

**Pattern:** Annual release, sliding window between Dec and July (no fixed month). The brief assumed "Feb-March" — that's incorrect; the most recent two releases were **January 2026** and **July 2025**.

### Implications for `latest()`
- Cannot hardcode a release month. Must resolve `latest()` by:
  1. Hitting `package_show` to get the resource list
  2. Filtering resources whose name matches `r"WGEA.*Public Data File"`
  3. Sorting by `last_modified` desc
  4. Returning the top hit's `reporting_year`
- Cache for 30 days, but ALSO check CKAN `metadata_modified` on cache miss — if changed, invalidate. Robust against off-cycle re-releases (e.g., the 2021 file was re-modified `2026-01-09`).

### Stale flag rule (for the trust contract)
- `stale=False` if the most recent dataset reporting year ends within last 18 months
- `stale=True` otherwise (signals "release expected soon, check again")

---

## 1e. Build Estimate & Architecture

### Architectural template: **asic-mcp** (CKAN backbone) + **ato-mcp** (fuzzy employer search)

| Component                  | Source sibling      | Reuse % |
| -------------------------- | ------------------- | ------- |
| CKAN client + caching      | asic-mcp            | ~90%    |
| Curated dataset registry   | abs-mcp / apra-mcp  | ~80%    |
| Field-annotated tool sigs  | abs-mcp/server.py   | ~95%    |
| Fuzzy name search (rapidfuzz) | ato-mcp           | ~85%    |
| Long-format pivot/filter   | ato-mcp shaping.py  | ~70%    |
| Trust contract / DataResponse | all siblings     | ~95%    |
| Test scaffolding           | apra-mcp (CC-BY 3.0 AU twin) | ~90% |

### Project sizing (vs siblings)

| Sibling   | server.py LOC | Test files | Tests       |
| --------- | ------------- | ---------- | ----------- |
| ato-mcp   | 905           | 23         | 288         |
| apra-mcp  | 737           | 16         | 263         |
| aihw-mcp  | —             | 15         | 258         |
| asic-mcp  | —             | 12         | 229         |
| abs-mcp   | 646           | 8          | —           |
| **wgea-mcp (target)** | **~700-800** | **~14-16** | **~150**  |

### Effort breakdown (2.5 days)

| Day      | Tasks                                                                    |
| -------- | ------------------------------------------------------------------------ |
| Day 1 AM | Scaffold from apra-mcp (closest twin), CKAN client, dataset registry     |
| Day 1 PM | Implement 5 tools with Field annotations, employer_name fuzzy search     |
| Day 2 AM | Long-format pivot/filter, industry aggregation, latest() year-resolver   |
| Day 2 PM | ≥100 unit tests, ≥6 live tests, examples/ demo prompts                   |
| Day 3 AM | Glama files (LICENSE/CHANGELOG/etc.), README cross-links, 10× pytest     |
| Day 3 PM | PyPI publish via OIDC, GitHub release, Glama submission, smoke verification |

### Risks identified

| Risk                                          | Likelihood | Mitigation |
| --------------------------------------------- | ---------- | ---------- |
| Schema drift between 2025 and older years     | High       | Ship v0.1.0 covering 2021-2025 only (CSV-era); document 2014-20 (XLSX-era) as future work |
| 71 MB ZIP / 952 MB uncompressed — disk usage | Medium     | Cache the ZIP, lazy-extract only the requested thematic CSV. Never bundle in wheel. |
| Cache TTL miss-handling on off-cycle re-release | Low       | Cross-check CKAN `metadata_modified` on every cache hit beyond 24h |
| PyPI new-project 5/24h limit                  | Low        | Already 5 published — wait 24h after most recent if a new account hits the cap |
| Fuzzy match false-positives ("CBA" → "CBA Group Pty Ltd" vs Commonwealth Bank) | Medium | Build hand-crafted alias list as eval set; tune rapidfuzz threshold > 80% as brief specifies |

---

## 2. Will Customers Pay? (Monetisation Analysis)

### Buyer personas with stated willingness to pay

| Persona                         | Use case                                              | Est. monthly WTP |
| ------------------------------- | ----------------------------------------------------- | ---------------- |
| **HR / DEI consultancies**      | "Benchmark client org against industry peers" reports | $50-200/mo       |
| **Boutique ESG reporting firms** | Sustainability + governance disclosures (mandatory 2026+ for AU listcos) | $100-300/mo |
| **Investigative journalists**   | "Worst pay gaps in [sector]" stories — annual release news cycle | $20-50/mo |
| **Executive search / recruiting** | Employer-brand intelligence for placements | $50-150/mo |
| **B2B SaaS (HR tech)**          | Embed "gender equity benchmark" in their dashboards   | $200-500/mo (API tier) |
| **Researchers / think tanks**   | Academic work, free tier sufficient                   | $0 (free tier)   |

### Demand signals (qualitative)
- WGEA release dates drive **front-page coverage** in AFR, SMH, ABC News every cycle (Jan 2026, Feb 2024, etc.)
- Companies actively publish pay gap rebuttals — they monitor this data
- DEI consulting is a **growing AU market** (Climate Active + mandatory ASRS reporting from 2026 means ESG analysts now need this data)
- "Pay gap at [my employer]" is a high-intent query — exactly the use case fuzzy search nails

### Why it's smaller TAM than ATO/APRA
- HR/DEI is a narrower vertical than tax (every business) or banking (every consumer)
- But **per-customer willingness to pay is higher** because the data is decision-critical and the alternative is paying an analyst to scrape the Data Explorer

### Alignment with your roadmap
This is **the prototype customer** for the x402 paywall plan in your post-MVP notes:
- Niche, high-value buyers
- Clearly defined query patterns that map to billable units
- News-driven traffic spikes (every Jan/Feb/March release week) → variable pricing opportunity

---

## 3. Recommendation

**BUILD wgea-mcp now, target 2.5 days, ship as v0.1.0.**

Reasons:
1. ✅ **Cleanest backend of any sibling** (single CKAN package, well-named resources, no auth, no rate limit)
2. ✅ **Cleanest licence** (CC-BY 3.0 AU, with explicit legislative redistribution intent)
3. ✅ **Lowest curation effort** (8 thematic CSVs already split the way we want)
4. ✅ **Reuses 80%+ of apra-mcp + asic-mcp + ato-mcp infrastructure**
5. ✅ **Strong monetisation fit** for the x402 paywall plan — niche, high-value, news-driven
6. ✅ **Annual cadence + auto-discovery** means low maintenance burden after launch

The only meaningful effort is the **employer_name fuzzy search + alias list** — and that's the work that creates the moat (a CSV anyone can download, but agentic search-by-employer-name is the value-add).

---

## ⏸️ PAUSE — Awaiting Approval to Proceed to Phase 2

**Open questions for Harry before build:**

1. Confirm **6 curated datasets** (vs the brief's 5-7) — `workforce_composition`, `pay_gap`, `industry_pay_gap`, `parental_leave`, `harm_prevention`, `gender_equality_actions`. OK to defer `employee_support` and `workplace_overview` to v0.2.0?
2. Confirm **v0.1.0 scope = CSV-era only (2021-2025)**, deferring 2014-2020 XLSX-era schema-mapper to v0.2.0? This unblocks the 2.5-day estimate.
3. Confirm **30-day TTL with CKAN `metadata_modified` cross-check** as the cache strategy? (Same as apra-mcp.)
4. Greenlight Phase 2?
