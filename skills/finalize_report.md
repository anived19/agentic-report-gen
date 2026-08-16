---
name: finalize_report
description: >
  Signals that data gathering and report format planning are complete.
  Transitions the master loop from RUNNING to DONE to begin Chief Editor synthesis.
tool_function: harness.orchestrator.finalize_report
parameters:
  type: object
  properties: {}
---

# Skill: finalize_report

## Purpose
Signals completion of the master orchestrator loop. Hands off gathered data and ReportSpec to Chief Editor and PDF compiler.
Cannot be called while validate_data reports unsatisfied required categories.
