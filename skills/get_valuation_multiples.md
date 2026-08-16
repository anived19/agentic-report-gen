---
name: get_valuation_multiples
description: >
  Fetches valuation multiples including trailing P/E, forward P/E, P/B, P/S,
  EV/EBITDA, dividend yield, revenue (TTM), gross margin, and operating margin.
tool_function: tools.finance_tools.get_valuation_multiples
parameters:
  type: object
  properties:
    ticker:
      type: string
      description: Validated stock ticker symbol (e.g. 'TCS.NS', 'AAPL').
  required:
    - ticker
---

# Skill: get_valuation_multiples

## Purpose
Deterministic valuation fetcher from yfinance. Returns key valuation ratios and revenue margins.
