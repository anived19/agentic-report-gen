---
name: reflect_on_progress
description: >
  Records a structured checkpoint on what has been gathered so far,
  what (if anything) is still missing relative to the editorial goal,
  and why the next planned action follows from that gap. Required at
  least once before finalize_report.
tool_function: harness.orchestrator.reflect_on_progress
parameters:
  type: object
  properties:
    gathered_summary:
      type: string
      description: Concise summary of what has been fetched so far and what it showed (e.g. specific values, sentiment direction, AML flags found).
    still_needed:
      type: array
      items:
        type: string
      description: Specific gaps remaining relative to the editorial_goal and report_type — empty array if nothing further is needed.
    next_action_rationale:
      type: string
      description: Why the next tool call (or finalize_report, if still_needed is empty) follows from gathered_summary and still_needed.
  required:
    - gathered_summary
    - still_needed
    - next_action_rationale
---

# Skill: reflect_on_progress

## Purpose
Forces an explicit self-report of fetch-stage state and rationale before
the model can finalize. No external cost — pure self-report, logged
verbatim in tool_log via the existing ToolCallRecord.arguments field.
