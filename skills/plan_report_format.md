---
name: plan_report_format
description: >
  Plans the custom structure, section ordering, and editorial emphasis for the
  final report based on what data was found and the user's intent.
  Produces a ReportSpec for the Chief Editor.
tool_function: harness.orchestrator.plan_report_format
parameters:
  type: object
  properties:
    rationale:
      type: string
      description: Detailed reasoning for why this layout and emphasis was selected for this report type and findings.
    sections:
      type: array
      items:
        type: object
        properties:
          key:
            type: string
            description: Section key (e.g. executive_summary, financial_highlights, valuation_analysis, sentiment_news, technicals, holdings, fundamentals_deep_dive, risk_factors, scenario_outlook).
          include:
            type: boolean
            description: Whether this section is included in the final report.
          emphasis:
            type: string
            description: Direct editorial instruction to the Chief Editor on what leads and what is background/footnote.
          order:
            type: integer
            description: 1-indexed display order for the section.
        required:
          - key
          - include
          - emphasis
          - order
      description: List of section specifications.
  required:
    - rationale
    - sections
---

# Skill: plan_report_format

## Purpose
Agentic report formatting tool. Customizes section inclusion, order, and emphasis directives for the Chief Editor.
Enables different framings (e.g. sentiment momentum vs valuation intrinsic value) for the same company.
