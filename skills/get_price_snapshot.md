---
name: get_price_snapshot
description: >
  Fetches current stock price, market capitalization, 50-day and 200-day
  moving averages, and period high/low price range for a ticker via yfinance.
tool_function: tools.finance_tools.get_price_snapshot
parameters:
  type: object
  properties:
    ticker:
      type: string
      description: Validated stock ticker symbol (e.g. 'TCS.NS', 'AAPL').
  required:
    - ticker
---

# Skill: get_price_snapshot

## Purpose
Deterministic market price fetcher. Sourced directly from yfinance. Returns price,
market cap, moving averages, and period high/low.
