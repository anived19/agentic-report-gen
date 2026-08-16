---
name: validate_data
description: >
  Performs a deterministic completeness and consistency check on all gathered
  data against the requirement profile for the current report type.
  Returns whether requirements are satisfied and any missing data categories.
tool_function: harness.orchestrator.validate_data
parameters:
  type: object
  properties: {}
---

# Skill: validate_data

## Purpose
Deterministic validation tool that compares accumulated AgentState against orchestrator_config.yaml requirements.
Reports satisfied: bool and lists missing categories.
