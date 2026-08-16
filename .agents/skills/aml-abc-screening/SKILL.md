---
name: aml-abc-screening
description: >
  Trigger this skill when you need to understand, extend, or debug the
  AML/ABC compliance screening layer (Layer 2). Covers: which data sources
  are screened, what each source covers, how to add a new source, how
  entities are derived, severity classification logic, and known data gaps.
tags:
  - aml
  - compliance
  - screening
  - layer2
  - sanctions
---

# Skill: AML / ABC Screening Layer

## When to activate this skill
Load this skill when:
- You need to add a new screening data source
- You need to understand how entities are selected for screening
- You need to adjust severity classification thresholds
- You need to debug a failing screening source
- You need to understand what the --aml flag does and doesn't cover

---

## Discovery scope: workspace (not global)

This skill lives at `.agents/skills/aml-abc-screening/` — workspace scope.
It is project-specific (references this project's schemas, source list, and
config) and should not be globally available.

---

## Pipeline overview

```
main.py --aml flag
    │
    ▼
harness/aml_agent.py  run_aml_screening()
    │
    ├── Phase 1: Structured sources (deterministic loop)
    │     tools/aml_tools.py:
    │       screen_ofac_sdn()          → OFAC SDN REST API
    │       screen_opensanctions()     → OpenSanctions search API
    │       screen_world_bank_debarred() → World Bank IVP JSON API
    │       screen_un_sanctions()      → UN Consolidated List XML (cached)
    │       screen_eu_sanctions()      → EU Financial Sanctions XML (cached)
    │       screen_sec_fcpa()          → SEC EDGAR full-text search
    │
    ├── Phase 2: Adverse media (bounded Tavily loop)
    │     harness/aml_agent_loop.py  run_aml_adverse_media_agent()
    │       LLM formulates queries → screen_entity_aml tool
    │       → skills/screen_entity_aml.md
    │       → tools/aml_tools.py:search_aml_adverse_media()
    │       → tools/search_tools.py:search_web_news()  [existing tool]
    │     Keyword-based severity classification (no LLM paraphrasing)
    │
    ├── Phase 3: Jurisdictional context (deterministic)
    │     tools/aml_tools.py:
    │       get_jurisdictional_risk() → TI CPI 2023 hardcoded snapshot
    │       get_fatf_risk()           → FATF grey/black list snapshot (Oct 2024)
    │     Country inferred from ticker suffix (.NS/.BO = India) or currency
    │
    ▼
schemas.py  AMLScreeningResult  →  harness/synthesis.py  render_aml_markdown()
    │                                  (deterministic — no LLM involved)
    ▼
FinalReport.markdown_body  (appended after Layer 1 Markdown)
    ▼
tools/pdf_tools.py  compile_pdf()
```

---

## Data source reference

| Source | Type | Coverage | Update frequency | API docs |
|---|---|---|---|---|
| OFAC SDN List | REST API (free) | US-designated terrorists, narcos, proliferators | Real-time | [Link](https://sanctionslist.ofac.treas.gov/) |
| OpenSanctions | Search API (free tier) | 100+ global sanctions & watchlists aggregated | Daily | [Link](https://api.opensanctions.org/) |
| World Bank Debarred | JSON API (free) | Procurement fraud / debarment | As published | [Link](https://apigwext.worldbank.org/) |
| UN Consolidated List | XML download (free, cached) | UN Security Council asset freezes | Weekly | [Link](https://scsanctions.un.org/) |
| EU Financial Sanctions | XML download (free, cached) | EU council asset freezes & travel bans | Daily | [Link](https://data.europa.eu/euodp/en/data/dataset/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions) |
| SEC EDGAR FCPA | Full-text search (free) | FCPA enforcement / litigation releases | Real-time | [Link](https://efts.sec.gov/LATEST/search-index) |
| TI CPI 2023 | Hardcoded snapshot | Country-level corruption perception index | Annual (manual refresh) | [Link](https://www.transparency.org/en/cpi/2023) |
| FATF Grey/Black List | Hardcoded snapshot | Jurisdictions under increased monitoring | Tri-annual (manual refresh) | [Link](https://www.fatf-gafi.org/) |
| Tavily adverse media | Search API (existing key) | Regulatory press releases, adverse news | Real-time | — |

### Known data gaps (documented, not silently omitted)

| Source | Gap | Reason | Workaround |
|---|---|---|---|
| MCA/ROC India filings | Not available | No machine-readable free API for structured director data | Tavily search + manual review |
| RBI Wilful Defaulter list | Not available | Published as PDFs by individual banks; no central index | Manual review |
| ICIJ Offshore Leaks | Not integrated | Large dataset requiring local indexing; feasible as future enhancement | — |
| NSE/BSE shareholding filings | Not available | Structured API is exchange-gated | Yahoo Finance holdings (partial) |
| Individual FII/DII breakdown | Not available | Requires BSE/NSE filings API | Combined institutional % only |

---

## Severity classification

### Structured sources (Phase 1)
| Severity | Meaning |
|---|---|
| 🟢 None | Source searched, no name match found |
| 🟡 Watch | API/network error — screening incomplete; manual check needed |
| 🟠 Elevated | Partial name match or presence in secondary lists (OpenSanctions, UN, EU) |
| 🔴 High | Confirmed presence on OFAC SDN or World Bank debarment list |

### Adverse media (Phase 2)
Severity is classified by keyword presence in the retrieved content:
- **High:** "sanctioned", "convicted", "indicted", "money laundering", "wilful default"
- **Elevated:** "SEBI order", "bribery", "corruption", "FCPA", "SFO investigation", "fraud"
- **Watch:** Any result returned from an AML query that doesn't hit the above keywords

---

## How to add a new screening source

1. **Implement a screener function** in `tools/aml_tools.py`:
   ```python
   def screen_new_source(entity_name: str) -> AMLFinding:
       # fetch, match, return AMLFinding(...)
   ```
2. **Add it to `_STRUCTURED_SCREENERS`** in `harness/aml_agent.py`:
   ```python
   from tools.aml_tools import screen_new_source
   _STRUCTURED_SCREENERS = [..., screen_new_source]
   ```
3. **Document it** in the source reference table above and in `ARCHITECTURE.md`.
4. **Update `render_config.yaml`** section_specs if the source warrants a config entry.

---

## Key files

| File | Role |
|---|---|
| `harness/aml_agent.py` | Orchestrator: entity derivation + phase coordination |
| `harness/aml_agent_loop.py` | Bounded Tavily loop for adverse media (Phase 2) |
| `tools/aml_tools.py` | All structured screening functions + TI CPI / FATF snapshots |
| `agents/aml_screener.md` | System prompt for the adverse media search agent |
| `skills/screen_entity_aml.md` | Gemini tool-calling skill spec for the adverse media search |
| `schemas.py` | `AMLFinding`, `AMLSeverity`, `AMLScreeningResult` |
| `harness/synthesis.py` | `render_aml_markdown()` — deterministic Markdown formatter |
