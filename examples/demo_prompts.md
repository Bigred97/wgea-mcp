# Demo prompts

Five plain-English prompts an agent can answer with wgea-mcp.

---

## 1. "What's the gender breakdown at Westpac?"

```
search_datasets("workforce composition")
# → [{id: "WORKFORCE_COMPOSITION", ...}]

get_data("WORKFORCE_COMPOSITION",
         filters={"employer_name": "Westpac"})
# → 30+ rows × gender × occupation × manager_category × n_employees
```

---

## 2. "Top 10 employers in mining by workforce size"

```
get_data("WORKFORCE_COMPOSITION",
         filters={"anzsic_division": "Mining"},
         max_rows=5000)
# → all mining-industry rows; the agent aggregates n_employees per employer
# and ranks.
```

---

## 3. "Which Financial-and-Insurance employers conducted a pay-gap analysis in 2024-25?"

```
get_data("GENDER_EQUALITY_ACTIONS",
         filters={
             "anzsic_division": "Financial and Insurance Services",
             "subsection": "Gender Pay Gap",
             "response": "Yes"
         })
# → one row per (employer × question) where response=Yes
```

---

## 4. "Compare CBA's promotions to manager by gender, 2023-24 vs 2024-25"

```
get_data("WORKFORCE_MANAGEMENT",
         filters={
             "employer_name": "CBA",
             "movement_type": "Promotions",
             "manager_category": "Managers"
         },
         start_period="2023-24",
         end_period="2024-25")
# → rows × gender × manager_type × n_employees
# Agent groups by reporting_year + gender and compares.
```

---

## 5. "Pay gap by occupation: senior managers, all of professional services"

```
get_data("WORKFORCE_COMPOSITION",
         filters={
             "anzsic_division": "Professional, Scientific and Technical Services",
             "occupation": "Senior Managers"
         },
         format="csv")
# → CSV of every senior-manager bucket in the sector.
# Agent computes pay gap proxies (e.g. % women in senior manager roles).
```

---

> **Note on the headline pay-gap %.** WGEA's Data Explorer publishes a
> per-employer "gender pay gap" headline number computed from confidential
> remuneration data. That specific aggregate is NOT in the public data
> file — WGEA pre-aggregates before public release. Use this MCP for
> the underlying workforce composition + policy detail; use the WGEA
> Data Explorer for the headline pay-gap percentage.
