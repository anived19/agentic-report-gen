---
name: pdf-report-generator
description: >
  Trigger this skill when you need to understand, modify, or extend the PDF
  rendering pipeline for the Financial Report Generator. Covers: how the
  Markdown-to-HTML-to-PDF pipeline works, how to add or reorder sections
  without touching render code, and how Layer 1 and Layer 2 outputs are
  assembled into a single document.
tags:
  - pdf
  - rendering
  - report
  - layer1
  - layer2
---

# Skill: PDF Report Generator

## When to activate this skill
Load this skill when:
- You need to change what sections appear in a report, or their order
- You need to understand how the Markdown body is assembled
- You need to add a new report section (e.g., a Layer 3)
- You need to debug a PDF rendering failure
- You need to change the visual layout or stylesheet

Do NOT load this skill for: data fetching, LLM model changes, or the
Research Agent loop — those are separate concerns.

---

## Discovery scope: why `.agents/skills/` (workspace), not `~/.gemini/config/skills/` (global)

This skill lives at `.agents/skills/pdf-report-generator/` relative to the
project root. This is the **workspace customization scope**, which means:

- It is discoverable only when working inside this project directory.
- It does not pollute the global skill list for other projects.
- Any developer who clones this repo gets the skill automatically.

The global scope (`~/.gemini/config/skills/`) is for skills that apply
across all your projects (e.g., a general Python debugging skill). PDF
rendering is project-specific — it references project-specific files,
config, and data models — so workspace scope is correct here.

---

## Architecture overview

```
User query
    │
    ▼
harness/synthesis.py  ← reads render_config.yaml for section list
    │  run_chief_editor() → Markdown (Layer 1)
    │  render_aml_markdown() → Markdown (Layer 2, if --aml)
    ▼
main.py  ← concatenates Layer 1 + Layer 2 Markdown into FinalReport.markdown_body
    ▼
tools/pdf_tools.py
    │  compile_pdf() → calls _render_full_html() → Jinja2 render
    │                → calls _write_pdf_weasyprint() or _write_pdf_xhtml2pdf()
    ▼
templates/report_template.html  ← Jinja2 template
static/report.css               ← stylesheet (inlined into HTML via {{ css | safe }})
```

### Why CSS is inlined (not linked)

PDF engines (especially `xhtml2pdf`) don't follow `<link href>` references
when generating from a string. The CSS is inlined via `{{ css | safe }}`.
The `safe` filter is load-bearing: without it, Jinja2's autoescaping
HTML-entity-escapes quote characters in font names, which silently breaks
xhtml2pdf's CSS parser.

---

## How to add or reorder sections (no code change needed)

Edit `render_config.yaml` in the project root:

```yaml
report_types:
  equity:
    layer1_sections:
      - executive_summary
      - financial_highlights
      - fundamentals_deep_dive
      - technicals
      - holdings
      - valuation_analysis
      - sentiment_news
      - risk_factors
      - scenario_outlook   # ← move this line to change position
    layer2_sections:
      - aml_abc_screening
```

Each key in `layer1_sections` maps to:
1. A heading string in `harness/synthesis.py:_SECTION_HEADINGS`
2. An instruction builder function in `harness/synthesis.py:_SECTION_INSTRUCTION_MAP`

The PDF renderer doesn't read section config — it renders whatever Markdown
the Chief Editor produced. Section config only affects what the Chief Editor
is instructed to write.

---

## How to add a new section

1. **Add the section key** to the appropriate `layer1_sections` list in `render_config.yaml`.
2. **Add a heading** in `harness/synthesis.py:_SECTION_HEADINGS`:
   ```python
   "my_new_section": "## My New Section Title",
   ```
3. **Add an instruction builder** in `harness/synthesis.py:_SECTION_INSTRUCTION_MAP`:
   ```python
   def _instr_my_new_section(outlook_label: str) -> str:
       return "Write a My New Section covering: ..."
   
   _SECTION_INSTRUCTION_MAP["my_new_section"] = _instr_my_new_section
   ```
4. If the new section needs **new data fields**, add them to `schemas.py:MarketMetrics`
   and fetch them in `tools/finance_tools.py`. No other files need changing.

---

## How to add a Layer 3

1. Create a new screening/data module (e.g., `tools/esg_tools.py`).
2. Create a new agent loop (e.g., `harness/esg_agent.py`) mirroring `harness/aml_agent.py`.
3. Add a new skill spec in `skills/` for any new Gemini-callable tool.
4. Add the layer 3 section key to `render_config.yaml:layer2_sections` (or a new `layer3_sections`).
5. Add a deterministic Markdown renderer in `harness/synthesis.py` (like `render_aml_markdown`).
6. Wire the new flag into `main.py` following the `--aml` pattern.
7. Create a `.agents/skills/` skill document for the new layer.

---

## PDF engine fallback

`tools/pdf_tools.py` tries `PDF_ENGINE` from `.env` (default: `weasyprint`).
On failure, it automatically falls back to `xhtml2pdf`. Both render the same
HTML/CSS; WeasyPrint produces better output but requires system-level
Pango/Cairo/GDK-PixBuf libraries that may not install cleanly on Windows.

To force a specific engine: set `PDF_ENGINE=xhtml2pdf` in `.env`.

---

## Template variables reference

| Variable | Type | Description |
|---|---|---|
| `title` | str | `<title>` tag content |
| `ticker` | str | Exchange ticker symbol |
| `company_name` | str | Full company name |
| `generated_at` | str | ISO date string |
| `chart_data_uri` | str \| None | Base64 PNG data URI for the price chart |
| `body_html` | str | Rendered Markdown → HTML (the full report body) |
| `css` | str | Full CSS file contents (inlined) |
| `has_aml` | bool | True if AML screening was run; controls AML disclaimer block |
| `outlook_months` | int | Configurable price-window length (default 6) |

---

## Key files

| File | Role |
|---|---|
| `render_config.yaml` | Section ordering per report type — edit here to change layout |
| `harness/synthesis.py` | Section instruction builders + `render_aml_markdown()` |
| `tools/pdf_tools.py` | HTML assembly and PDF engine wrapper |
| `templates/report_template.html` | Jinja2 HTML shell |
| `static/report.css` | Print-oriented stylesheet |
| `tools/chart_tools.py` | Price + MA chart → base64 PNG |
