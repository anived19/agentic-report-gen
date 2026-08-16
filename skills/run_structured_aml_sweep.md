---
name: run_structured_aml_sweep
description: >
  Runs a comprehensive, bundled deterministic AML/ABC screening sweep across
  all structured regulatory databases (OFAC SDN, OpenSanctions, World Bank Debarred,
  UN Sanctions, EU Sanctions, SEC EDGAR FCPA, TI CPI, and FATF).
tool_function: tools.aml_tools.run_structured_aml_sweep
parameters:
  type: object
  properties:
    entity_name:
      type: string
      description: Primary legal entity name to screen.
    ticker:
      type: string
      description: Stock ticker symbol to derive secondary entity and jurisdictional context.
      default: ""
  required:
    - entity_name
---

# Skill: run_structured_aml_sweep

## Purpose
Bundled deterministic screening tool across 8 regulatory compliance databases.
Returns list of structured AML findings with severity levels and citations.
Does not count against Tavily search budget.
