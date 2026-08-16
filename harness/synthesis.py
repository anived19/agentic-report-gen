"""
Chief Editor: single-shot synthesis call. No tools, no autonomy, no
multi-turn loop — deliberately not agentic.

There's no decision left to make at this stage: Market Data and Sentiment
Findings have already been fetched and validated. Giving this stage tool
access or iterative autonomy would only add a surface for it to restate a
number incorrectly, with no corresponding benefit. It reads the two
validated JSON objects and produces Markdown — nothing more.

The report_type parameter controls which sections the Chief Editor is
instructed to include. Section ordering is now driven by render_config.yaml
rather than hardcoded strings — adding a new section or changing order
requires only an edit to that config file, not a code change here.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from google import genai
from google.genai import types

from config import settings
from harness.gemini_retry import generate_with_retry
from harness.md_loader import load_agent_prompt
from schemas import (
    AMLFinding,
    AMLScreeningResult,
    MarketMetrics,
    ReportSpec,
    ReportType,
    SentimentFindings,
)

logger = logging.getLogger(__name__)

# Load render configuration once at module import time.
# Falls back to a minimal inline config if the file is missing, so an
# absent render_config.yaml degrades gracefully rather than crashing.
_RENDER_CONFIG_PATH = Path("render_config.yaml")

def _load_render_config() -> dict:
    if _RENDER_CONFIG_PATH.exists():
        try:
            with open(_RENDER_CONFIG_PATH, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Could not load render_config.yaml: %s — using inline defaults", exc)
    return {}

_RENDER_CONFIG: dict = _load_render_config()


# ---------------------------------------------------------------------------
# Section heading map (maps config key -> Markdown heading text)
# Keep in sync with render_config.yaml section_specs.
# ---------------------------------------------------------------------------
_SECTION_HEADINGS: dict[str, str] = {
    "executive_summary":       "# Executive Summary",
    "financial_highlights":    "## Financial Highlights",
    "fundamentals_deep_dive":  "## Fundamentals Deep-Dive",
    "technicals":              "## Technical Analysis",
    "holdings":                "## Ownership & Holdings",
    "valuation_analysis":      "## Valuation Analysis",
    "sentiment_news":          "## Market Sentiment & News",
    "risk_factors":            "## Risk Factors",
    "scenario_outlook":        "## {n}-Month Outlook",   # {n} filled at runtime
}

# ---------------------------------------------------------------------------
# Section instruction builders — one function per section type.
# Each returns a plain-English instruction string fed to the Chief Editor.
# ---------------------------------------------------------------------------

def _instr_executive_summary(outlook_label: str) -> str:
    return (
        "Write a concise Executive Summary (3–5 sentences) covering: the company's "
        "current market position, overall sentiment call, and the primary driver of "
        "the near-term outlook. Do not repeat numbers that appear in other sections — "
        "the summary should read as a standalone verdict, not a data recitation."
    )

def _instr_financial_highlights(outlook_label: str) -> str:
    return (
        f"Write a Financial Highlights table: Metric | Value | Notes. "
        f"Rows: Current Price, Market Cap, 50-Day MA, 200-Day MA, "
        f"{outlook_label} High, {outlook_label} Low. "
        f"For Market Cap and currency figures, use human-readable financial units (e.g. using market_cap_formatted "
        f"such as '₹17.73 Lakh Cr' or '$2.50T' — NEVER display raw scientific notation like '1.77e+13'). "
        f"Notes: one-line factual context (e.g. 'price above 50d MA'). "
        f"Source note on table caption: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_fundamentals_deep_dive(outlook_label: str) -> str:
    return (
        "Write a Fundamentals Deep-Dive section with three sub-tables:\n"
        "1. Key Metrics table (Metric | Value | Notes): EPS (TTM), Dividend Yield, "
        "   Debt-to-Equity, Return on Equity (ROE), Return on Capital Employed (ROCE). "
        "   For any field in unavailable_fields, write 'data unavailable' in the Value column.\n"
        "2. Analyst Consensus table (Metric | Value): Buy count, Hold count, Sell count, "
        "   Mean price target, High price target, Low price target, Recommendation. "
        "   If analyst fields are unavailable, say so. Cite Yahoo Finance as the source.\n"
        "3. Quarterly Financials table (Quarter | Revenue | Net Income | Rev QoQ % | Profit QoQ %): "
        "   use the quarterly_financials array (newest first). If empty, state 'data unavailable'. "
        "   IMPORTANT FORMATTING: Format all absolute currency numbers in standard human-readable financial scales "
        "   (e.g. '₹71,714 Cr' or '₹7.17 Lakh Cr' or '$15.2B' — NEVER display unscaled 12-digit numbers like '761,700,000,000.00'). "
        "   If Net Income exceeds Revenue in a quarter (due to exceptional one-off gains, demergers, or discontinued operations), "
        "   add an explanatory asterisk note below the table: '*(Note: Net income for [Quarter] includes extraordinary/one-off items or demerger accounting gains)*'. "
        "   Source note: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_technicals(outlook_label: str) -> str:
    return (
        "Write a Technical Analysis section covering:\n"
        "- RSI-14: state the exact value, then interpret (>70 overbought, <30 oversold, 30–70 neutral). "
        "  These levels are statistical reference points, not trading signals.\n"
        "- MACD (12/26/9): state line, signal, and histogram values. "
        "  Interpret as bullish/bearish momentum only if the numbers clearly support it.\n"
        "- Volume trend: state whether volume is rising, falling, or flat vs. the 60-day average, "
        "  and what that implies in context of the price direction.\n"
        f"- Support & Resistance: state the derived levels (10th/90th percentile of the "
        f"  {outlook_label.lower()} price range). Note: 'These are statistically derived "
        f"  reference levels, not broker recommendations.'\n"
        "For any unavailable technical field, state it explicitly — do not omit or estimate."
    )

def _instr_holdings(outlook_label: str) -> str:
    return (
        "Write an Ownership & Holdings section:\n"
        "- Table: Holder Category | % Held. Rows: Promoter/Insider, Institutional "
        "  (combined FII+DII — note Yahoo Finance does not break these out separately), Public.\n"
        "- For each unavailable field, write 'data unavailable'.\n"
        "- Add a one-sentence note: 'Institutional figure is the combined FII+DII total "
        "  as reported by Yahoo Finance. Individual FII and DII breakdown requires "
        "  BSE/NSE exchange filings and is not available through this data source.'\n"
        "Source: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_valuation_analysis(outlook_label: str) -> str:
    return (
        "Write a Valuation Analysis table: Metric | Value | Notes. "
        "Rows: P/E (Trailing), P/E (Forward), Price-to-Book, Price-to-Sales, "
        "EV/EBITDA, Dividend Yield, EPS (TTM), Revenue (TTM), Gross Margin, "
        "Operating Margin. "
        "For Revenue (TTM) and currency figures, use human-readable financial units (e.g. revenue_ttm_formatted "
        "such as '₹9.50 Lakh Cr' or '$380.00B' — NEVER display raw scientific notation like '1.77e+13'). "
        "Notes: add brief interpretive context ONLY when the sentiment findings "
        "contain explicit analyst commentary that supports the interpretation — "
        "otherwise leave Notes blank. "
        "Source note: '(Source: Yahoo Finance via yfinance)'"
    )

def _instr_sentiment_news(outlook_label: str) -> str:
    return (
        "Write a Market Sentiment & News section:\n"
        "- Open with the overall_sentiment label and sentiment_summary (1–2 sentences).\n"
        "- Key Catalysts sub-heading: bulleted list of key_catalysts, each ending in "
        "  '[Source: URL]' using the exact source_url from the JSON. Do not alter URLs.\n"
        "- Key Risks sub-heading: same format for key_risks.\n"
        "Do not introduce any claim not in the sentiment findings JSON."
    )

def _instr_risk_factors(outlook_label: str) -> str:
    return (
        "Write a Risk Factors section (separate from the Outlook):\n"
        "- List 3–5 specific risks drawn from the key_risks in the sentiment findings.\n"
        "- Each bullet: the risk in one sentence, followed by '[Source: URL]'.\n"
        "- Do not pad with generic market-risk boilerplate.\n"
        "- If key_risks is empty or uncited, state: 'No specific cited risk factors "
        "  were identified in this research cycle. This does not imply the absence of risk.'"
    )

def _instr_scenario_outlook(outlook_label: str) -> str:
    return (
        f"Write a {outlook_label} Outlook section using the Bull/Base/Bear structure:\n"
        "**Bull Case** (2–4 sentences): describe the upside scenario tied to a specific "
        "catalyst or metric already in this report (e.g. RSI level, MACD crossover, "
        "a cited positive catalyst). Use hedged language ('could', 'may', 'if X materializes').\n"
        "**Base Case** (2–4 sentences): the most likely path based on current data — "
        "balanced view of technicals + sentiment + valuation. Hedged language required.\n"
        "**Bear Case** (2–4 sentences): the downside scenario tied to a specific cited "
        "risk or technical weakness. Hedged language required.\n"
        "Close with one sentence: 'This outlook is an analytical synthesis of current "
        "publicly available data and does not constitute a prediction or investment advice.'"
    )


_SECTION_INSTRUCTION_MAP = {
    "executive_summary":       _instr_executive_summary,
    "financial_highlights":    _instr_financial_highlights,
    "fundamentals_deep_dive":  _instr_fundamentals_deep_dive,
    "technicals":              _instr_technicals,
    "holdings":                _instr_holdings,
    "valuation_analysis":      _instr_valuation_analysis,
    "sentiment_news":          _instr_sentiment_news,
    "risk_factors":            _instr_risk_factors,
    "scenario_outlook":        _instr_scenario_outlook,
}


def _build_section_instructions(
    report_type: ReportType,
    outlook_label: str,
    report_spec: Optional[ReportSpec] = None,
) -> str:
    """
    Build the ordered list of section instructions for the Chief Editor.
    If a ReportSpec is provided by the orchestrator, its sections and emphasis directives
    override the default render_config.yaml list.
    """
    if report_spec and report_spec.sections:
        active_sections = sorted(
            [s for s in report_spec.sections if s.include],
            key=lambda x: x.order,
        )
        lines: list[str] = [
            f"This is a {report_type.value.upper()} report with custom agentic formatting.",
            f"Include EXACTLY these sections in EXACTLY this order, following the specific emphasis directives:",
        ]
        for spec in active_sections:
            heading = _SECTION_HEADINGS.get(spec.key, f"## {spec.key.replace('_', ' ').title()}")
            heading = heading.replace("{n}", str(outlook_label.split("-")[0]))
            instruction_fn = _SECTION_INSTRUCTION_MAP.get(spec.key)
            base_instruction = instruction_fn(outlook_label) if instruction_fn else ""
            emphasis_directive = f"\n[EDITORIAL EMPHASIS DIRECTIVE]: {spec.emphasis}" if spec.emphasis else ""
            lines.append(f"\n{heading}\n{base_instruction}{emphasis_directive}")
        return "\n".join(lines)

    # Fallback to render_config.yaml
    config_sections: list[str] | None = None
    try:
        config_sections = (
            _RENDER_CONFIG
            .get("report_types", {})
            .get(report_type.value, {})
            .get("layer1_sections")
        )
    except Exception:
        pass

    # Inline defaults (mirrors render_config.yaml) — used if config file is absent
    _defaults: dict[ReportType, list[str]] = {
        ReportType.SENTIMENT: [
            "executive_summary", "sentiment_news", "risk_factors", "scenario_outlook",
        ],
        ReportType.VALUATION: [
            "executive_summary", "financial_highlights", "fundamentals_deep_dive",
            "valuation_analysis", "scenario_outlook",
        ],
        ReportType.EQUITY: [
            "executive_summary", "financial_highlights", "fundamentals_deep_dive",
            "technicals", "holdings", "valuation_analysis",
            "sentiment_news", "risk_factors", "scenario_outlook",
        ],
        ReportType.GENERAL: [
            "executive_summary", "financial_highlights",
            "sentiment_news", "risk_factors", "scenario_outlook",
        ],
    }

    sections = config_sections or _defaults.get(report_type, _defaults[ReportType.GENERAL])

    lines: list[str] = [
        f"This is a {report_type.value.upper()} report. "
        f"Include EXACTLY these sections in EXACTLY this order:"
    ]
    for section_key in sections:
        heading = _SECTION_HEADINGS.get(section_key, f"## {section_key.replace('_', ' ').title()}")
        heading = heading.replace("{n}", str(outlook_label.split("-")[0]))
        instruction_fn = _SECTION_INSTRUCTION_MAP.get(section_key)
        instruction = instruction_fn(outlook_label) if instruction_fn else ""
        lines.append(f"\n{heading}\n{instruction}")

    return "\n".join(lines)


def run_chief_editor(
    market_metrics: MarketMetrics,
    sentiment_findings: SentimentFindings,
    report_type: ReportType = ReportType.GENERAL,
    report_spec: Optional[ReportSpec] = None,
) -> str:
    """Synthesize validated market data + sentiment findings into the final report Markdown."""
    client = genai.Client(api_key=settings.gemini_api_key)
    system_prompt = load_agent_prompt("chief_editor")

    outlook_label = f"{market_metrics.outlook_months}-Month"
    section_instruction = _build_section_instructions(report_type, outlook_label, report_spec=report_spec)

    user_message = (
        f"Report type: {report_type.value}\n"
        f"Outlook window: {outlook_label}\n\n"
        f"{section_instruction}\n\n"
        "Compile the final report from the following already-verified data. "
        "Do not invent or alter any number — every figure below has already "
        "been validated; state a field as unavailable if it's listed in "
        "unavailable_fields, rather than guessing a value for it.\n\n"
        f"MARKET METRICS (JSON):\n{market_metrics.model_dump_json(indent=2)}\n\n"
        f"SENTIMENT FINDINGS (JSON):\n{sentiment_findings.model_dump_json(indent=2)}\n"
    )

    response = generate_with_retry(
        client,
        model=settings.gemini_model,
        contents=[types.Content(role="user", parts=[types.Part(text=user_message)])],
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )

    markdown_body = (response.text or "").strip()
    if not markdown_body:
        raise ValueError("Chief Editor returned empty output — check the model response/safety filters")

    logger.info("Chief Editor produced %d characters of Markdown", len(markdown_body))
    return markdown_body


def _finding_sort_key(finding: AMLFinding) -> tuple[int, str, str]:
    """
    Sort key for AML findings:
      1. High severity (🔴 High)
      2. Elevated severity (🟠 Elevated)
      3. Watch severity with actual substantive content (🟡 Watch)
      4. Clear / No match findings (🟢 None)
      5. Fetch errors / Network failures pushed to the bottom
    """
    summary_l = finding.finding_summary.lower()
    is_failure = any(
        kw in summary_l
        for kw in (
            "could not be completed",
            "could not fetch",
            "screener error",
            "401 client error",
            "404 client error",
            "unauthorized",
            "manual check recommended",
        )
    ) and not any(kw in summary_l for kw in ("name match found", "potential match", "name string found"))

    if is_failure:
        priority = 50
    elif finding.severity.value == "High":
        priority = 10
    elif finding.severity.value == "Elevated":
        priority = 20
    elif finding.severity.value == "Watch":
        priority = 30
    else:  # "None"
        priority = 40

    return (priority, finding.entity_screened, finding.source_name)


def render_aml_markdown(aml_result: AMLScreeningResult) -> str:
    """
    Render the AML/ABC screening result as a Markdown section.
    Called separately from run_chief_editor — AML content is never passed
    through the LLM; it is formatted deterministically from validated data.
    This prevents the model from paraphrasing or altering screening findings.
    """
    lines: list[str] = [
        "---",
        "",
        "# AML / ABC Compliance Screening",
        "",
        f"**Entities screened:** {', '.join(aml_result.entities_screened) or 'None'}  ",
        f"**Screened at:** {aml_result.screened_at.isoformat()}  ",
        "",
        "| Entity Screened | Source | Finding | Severity | Citation |",
        "|---|---|---|---|---|",
    ]

    severity_icons = {
        "None":     "🟢 None",
        "Watch":    "🟡 Watch",
        "Elevated": "🟠 Elevated",
        "High":     "🔴 High",
    }

    # Sort findings so critical hits & confirmed checks appear first,
    # clean results in the middle, and unreachable/failed sources at the bottom
    sorted_findings = sorted(aml_result.findings, key=_finding_sort_key)

    for finding in sorted_findings:
        icon = severity_icons.get(finding.severity.value, finding.severity.value)
        citation = f"[Link]({finding.source_url})" if finding.source_url else "—"
        lines.append(
            f"| {finding.entity_screened} "
            f"| {finding.source_name} "
            f"| {finding.finding_summary} "
            f"| {icon} "
            f"| {citation} |"
        )

    if not aml_result.findings:
        lines.append("| — | — | No findings generated | — | — |")

    lines += [
        "",
        "> **Compliance Disclaimer:** " + aml_result.disclaimer,
        "",
    ]
    return "\n".join(lines)

