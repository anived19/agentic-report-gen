---
name: compute_custom_financial_metric
description: >
  Safely evaluates custom mathematical and financial expressions inside an AST sandbox
  (e.g., 3-year CAGR, Free Cash Flow Yield, custom spreads, margins, or historical trends).
  Enforces 2-decimal rounding, explicit percentage formatting, and continuity validation.
tool_function: tools.finance_tools.compute_custom_financial_metric
parameters:
  type: object
  properties:
    expression:
      type: string
      description: Mathematical expression to evaluate (e.g. 'cagr(quarterly_revenues[0], quarterly_revenues[-1], 3)', 'fcf_yield(free_cash_flow, market_cap)', 'spread(current_price, fifty_day_ma)').
    ticker:
      type: string
      description: Optional stock ticker symbol (e.g. 'TCS.NS', 'TATAMOTORS.NS') to automatically populate historical quarterly and fundamental context.
    metric_name:
      type: string
      description: Optional descriptive name for the metric (e.g. '3Y_Revenue_CAGR', 'FCF_Yield', 'Price_50dMA_Spread').
    context:
      type: object
      description: Optional explicit numeric dictionary to provide variables for the calculation.
  required:
    - expression
---

# Skill: compute_custom_financial_metric

## Purpose
Enables autonomous calculation of ad-hoc financial metrics that aren't hardcoded in standard tools.
Supports `cagr(start, end, n)`, `fcf_yield(fcf, mcap)`, `margin(num, den)`, `spread(a, b)`, `min`, `max`, `sum`, `abs`, `round`, `pow`, `len`, and standard algebraic operators.
Enforces zero-division protection, negative base CAGR guardrails, and quarter continuity checks.
