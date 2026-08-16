---
name: screen_entity_aml
description: >
  Searches the live web for adverse regulatory, enforcement, and AML/ABC-related
  media about a company or entity. Returns a list of results with title, URL,
  and content snippet. Targets regulatory sources: SEBI, Enforcement Directorate,
  SEC, DOJ/FCPA, SFO, NCA, World Bank, UN sanctions. Every finding cited in
  the compliance report must trace to a URL this tool actually returned.
tool_function: tools.aml_tools.search_aml_adverse_media
parameters:
  type: object
  properties:
    query:
      type: string
      description: >
        A focused AML/ABC search query, e.g.:
        "Reliance Industries SEBI enforcement order"
        "TCS bribery corruption allegation"
        "HDFC Bank Enforcement Directorate raid money laundering"
        Prefer narrow regulatory-domain queries over broad company searches.
    max_results:
      type: integer
      description: Number of results to return (default 5, max 10).
      default: 5
  required:
    - query
---

# Skill: screen_entity_aml

## Purpose
The search tool used by the AML Screener agent to find regulatory press
releases, enforcement orders, and adverse media about a company or its
associated entities. Wraps the same Tavily search infrastructure as the
Research agent but with AML-focused query patterns.

## Usage notes
- Phrase queries as regulatory event searches, not financial sentiment queries.
  Good: "Reliance Industries SEBI adjudication 2023"
  Bad:  "Reliance Industries news"
- Aim for 2–4 focused queries covering: India regulatory (SEBI, ED),
  global enforcement (SEC FCPA, SFO, DOJ), and adverse media.
- The result URL is what gets cited in the compliance report. Use exact URLs
  returned — never fabricate or generalize.
- A search returning no results is a valid finding. Do not loop trying to
  find something that may not exist.
