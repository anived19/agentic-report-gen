---
name: get_technicals
description: >
  Fetches technical indicators including RSI-14, MACD (line, signal, histogram),
  20-day average volume, volume trend, and statistical support/resistance levels.
tool_function: tools.finance_tools.get_technicals
parameters:
  type: object
  properties:
    ticker:
      type: string
      description: Validated stock ticker symbol (e.g. 'TCS.NS', 'AAPL').
  required:
    - ticker
---

# Skill: get_technicals

## Purpose
Deterministic technical indicators calculator from price history. Returns RSI, MACD, volume trend, and support/resistance.
