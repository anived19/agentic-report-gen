---
name: search_adverse_media
description: >
  Searches the live web for adverse regulatory, enforcement, bribery, fraud,
  or corruption news about an entity using Tavily. Counts against the shared Tavily budget.
tool_function: tools.aml_tools.search_adverse_media
parameters:
  type: object
  properties:
    entity_name:
      type: string
      description: Company or entity name to screen.
    focus:
      type: string
      description: Optional targeted allegation or finding focus (e.g. 'reason for OFAC SDN listing', 'SEBI insider trading order').
      default: ""
    depth:
      type: string
      description: Search depth - 'basic' (1 credit) or 'advanced' (2 credits).
      enum:
        - basic
        - advanced
      default: basic
  required:
    - entity_name
---

# Skill: search_adverse_media

## Purpose
Adverse media search for regulatory compliance. Targets enforcement releases and adverse litigation.
Counts against the shared Tavily search budget.
