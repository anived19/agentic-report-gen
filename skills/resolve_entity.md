---
name: resolve_entity
description: >
  Resolves a natural language company or conglomerate query to a list of
  deduplicated, validated candidate entities with their tickers and exchanges.
tool_function: tools.ticker_resolver.resolve_entity
parameters:
  type: object
  properties:
    query:
      type: string
      description: Company name or group reference (e.g. 'Tata', 'Reliance Industries', 'Apple').
  required:
    - query
---

# Skill: resolve_entity

## Purpose
Entity and ticker resolver. Checks conglomerate mappings, static mappings, and yfinance search to return candidate entities.
