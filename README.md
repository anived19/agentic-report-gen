# Agentic Financial Report Generator

Takes a plain-English request and produces a professional PDF combining verified market data (yfinance) with cited live news sentiment (Tavily + Gemini). Optionally appends an AML/ABC compliance screening section via the `--aml` flag.

See `ARCHITECTURE.md` for the full pipeline design and `GLOSSARY.md` for plain-English definitions of agentic concepts.

## What's in the report

**Layer 1 — Financial Research Report** (always generated):
- Executive Summary
- Financial Highlights (price, market cap, 50d/200d MA, period high/low)
- Fundamentals Deep-Dive (EPS, D/E, ROE, ROCE, analyst consensus, quarterly financials)
- Technical Analysis (RSI-14, MACD, volume trend, support/resistance)
- Ownership & Holdings (insider %, institutional %)
- Valuation Analysis (P/E, forward P/E, P/B, P/S, EV/EBITDA, margins)
- Market Sentiment & News (cited catalysts and risks from live web research)
- Risk Factors (separately listed, each source-cited)
- Scenario Outlook (Bull / Base / Bear, each tied to a specific metric or catalyst)

*Which sections appear in which report type is controlled by `render_config.yaml`.*

**Layer 2 — AML/ABC Compliance Screening** (`--aml` flag):
- Structured screening table: entity → source → finding → severity → citation
- Sources: OFAC SDN, OpenSanctions, World Bank Debarred, UN Consolidated List, EU Sanctions, SEC EDGAR FCPA, TI CPI jurisdictional risk, FATF grey/black list, Tavily adverse media
- Compliance disclaimer (not investment advice, not AML/ABC clearance)

## Setup

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/macOS

pip install -r requirements.txt

# Edit .env — fill in GEMINI_API_KEY and TAVILY_API_KEY
```

### PDF engine — Windows note

`PDF_ENGINE=weasyprint` (default) gives the best output but needs system-level Pango/Cairo/GDK-PixBuf libraries. The pipeline auto-falls-back to `xhtml2pdf` (pure-pip, no native deps) if WeasyPrint fails. To force xhtml2pdf: set `PDF_ENGINE=xhtml2pdf` in `.env`.

## Run

```bash
# Layer 1 only
python main.py "full equity report on TCS"
python main.py "news sentiment report of Reliance Industries"
python main.py "valuation analysis of Apple"

# Layer 1 + Layer 2 (AML screening — adds ~30-60s)
python main.py "full equity report on TCS" --aml
python main.py "news sentiment report of HDFC Bank" --aml
```

Output PDF: `outputs/TICKER_YYYY-MM-DD.pdf`

## Extending

- **New report section**: edit `render_config.yaml` (add the key to the section list), add a heading to `_SECTION_HEADINGS` and an instruction builder to `_SECTION_INSTRUCTION_MAP` in `harness/synthesis.py`. See `.agents/skills/pdf-report-generator/SKILL.md`.
- **New AML screening source**: implement a function in `tools/aml_tools.py`, add it to `run_structured_aml_sweep` in `tools/aml_tools.py`. See `.agents/skills/aml-abc-screening/SKILL.md`.
- **New static ticker mappings**: add to `_STATIC_MAP` in `tools/ticker_resolver.py` or `tools/conglomerate_map.yaml`.
- **Change the model**: set `GEMINI_MODEL` in `.env`.
- **Change report tone/structure**: edit `agents/chief_editor.md` or `agents/orchestrator.md` — no code changes needed.
- **Change section ordering**: edit `render_config.yaml` — no code changes needed.
