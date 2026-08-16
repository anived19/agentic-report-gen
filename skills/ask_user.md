---
name: ask_user
description: >
  Pauses the agent loop and prompts the user with a question and a list of
  numbered options to disambiguate an entity when resolve_entity returns
  more than one candidate. THIS IS THE ONLY TOOL THAT PAUSES THE SYSTEM.
tool_function: harness.orchestrator.ask_user
parameters:
  type: object
  properties:
    question:
      type: string
      description: Clear question asking which entity or group company the user meant.
    options:
      type: array
      items:
        type: string
      description: List of candidate options (e.g. ['Tata Motors (TATAMOTORS.NS)', 'TCS (TCS.NS)']).
  required:
    - question
    - options
---

# Skill: ask_user

## Purpose
The sole human-in-the-loop interaction tool in the system.
Triggered ONLY when resolve_entity returns more than one candidate.
Pauses execution until the user selects an option.
