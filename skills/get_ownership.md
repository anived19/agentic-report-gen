---
name: get_ownership
description: >
  Fetches shareholding pattern percentages: promoter/insider %, institutional %,
  and public float %.
tool_function: tools.finance_tools.get_ownership
parameters:
  type: object
  properties:
    ticker:
      type: string
      description: Validated stock ticker symbol (e.g. 'TCS.NS', 'AAPL').
  required:
    - ticker
---

# Skill: get_ownership

## Purpose
Deterministic major holders extractor from yfinance. Returns insider %, institutional %, and public %.
