---
name: get_fundamentals
description: >
  Fetches company fundamental metrics including EPS (TTM), debt-to-equity,
  ROE, ROCE, and broker analyst consensus targets.
tool_function: tools.finance_tools.get_fundamentals
parameters:
  type: object
  properties:
    ticker:
      type: string
      description: Validated stock ticker symbol (e.g. 'TCS.NS', 'AAPL').
  required:
    - ticker
---

# Skill: get_fundamentals

## Purpose
Deterministic fundamental data fetcher from yfinance. Returns EPS, D/E, ROE, ROCE, and broker consensus.
