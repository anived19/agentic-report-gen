---
name: get_quarterly_financials
description: >
  Fetches quarterly revenue and net income financials for the last 4 quarters
  along with computed QoQ growth rates.
tool_function: tools.finance_tools.get_quarterly_financials
parameters:
  type: object
  properties:
    ticker:
      type: string
      description: Validated stock ticker symbol (e.g. 'TCS.NS', 'AAPL').
  required:
    - ticker
---

# Skill: get_quarterly_financials

## Purpose
Deterministic quarterly financials table extractor from yfinance. Returns last 4 quarters of revenue and net income.
