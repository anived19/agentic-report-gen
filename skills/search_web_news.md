---
name: search_web_news
description: >
  Searches the live web for recent news, analyst commentary, and market
  sentiment about a company or stock. Returns a list of results, each with
  a title, URL, and content snippet. Every claim you make based on these
  results must be attributed to the specific URL it came from.
tool_function: tools.search_tools.search_web_news
parameters:
  type: object
  properties:
    query:
      type: string
      description: >
        A focused search query — e.g. "Reliance Industries Q2 2026 earnings
        outlook" rather than just "Reliance Industries". Prefer several
        narrow queries over one broad one.
    ticker:
      type: string
      description: Optional ticker symbol to provide market context.
      default: ""
    depth:
      type: string
      description: Search depth - 'basic' (1 credit) or 'advanced' (2 credits).
      enum:
        - basic
        - advanced
      default: basic
    max_results:
      type: integer
      description: Number of results to return (default 5, max 10).
      default: 5
  required:
    - query
---

# Skill: search_web_news

## Purpose
Tavily web search tool for live news, sentiment, analyst price targets, and market catalysts/risks.
Counts against the shared Tavily budget of 5 calls per run.
