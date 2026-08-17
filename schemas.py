"""
Shared Pydantic schemas for data passed between pipeline stages.

Every value that crosses a stage boundary is validated against one of
these models before being trusted downstream. This is the concrete
enforcement mechanism behind the spec's "no hallucinated numbers, all
claims cited" constraint: if a stage's output doesn't parse, the pipeline
fails loudly instead of silently passing bad data to the PDF.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


# ---------------------------------------------------------------------------
# Report type: detected from the user's query in intake and threaded through
# the pipeline so both the Research Agent and Chief Editor adapt accordingly.
# ---------------------------------------------------------------------------

class ReportType(str, Enum):
    SENTIMENT  = "sentiment"   # news sentiment + 6-month outlook
    VALUATION  = "valuation"   # deep valuation multiples + analyst targets
    EQUITY     = "equity"      # comprehensive: sentiment + valuation + technicals
    GENERAL    = "general"     # catch-all when query doesn't match the others
    CUSTOM     = "custom"      # dynamic / freeform analytical angles


# ---------------------------------------------------------------------------
# Stage 1: Market Data
# Deterministic — populated directly from yfinance in tools/finance_tools.py.
# No LLM ever touches these fields, so there's no re-typing/paraphrasing
# step where a number could drift from its source.
# ---------------------------------------------------------------------------

class PricePoint(BaseModel):
    date: date
    close: float


class QuarterlyDataPoint(BaseModel):
    """One quarter's revenue and net income with computed growth rates."""
    quarter: str                            # e.g. "Q1 FY2025"
    revenue: Optional[float] = None         # in absolute units (as reported)
    revenue_formatted: Optional[str] = None # e.g. "Rs. 72,275 Cr"
    net_income: Optional[float] = None
    net_income_formatted: Optional[str] = None # e.g. "Rs. 13,349 Cr"
    revenue_growth_qoq: Optional[float] = None   # quarter-over-quarter % change
    revenue_growth_qoq_formatted: Optional[str] = None # e.g. "+2.23%"
    revenue_growth_yoy: Optional[float] = None   # year-over-year % change
    profit_growth_qoq: Optional[float] = None
    profit_growth_qoq_formatted: Optional[str] = None  # e.g. "-2.69%"
    profit_growth_yoy: Optional[float] = None
    data_gap_note: Optional[str] = None     # populated if a gap is detected between reporting periods


def format_currency_amount(amount: Optional[float], currency: Optional[str] = None) -> Optional[str]:
    """
    Format large numbers into standard financial scale strings.
    For INR: Lakh Cr, Cr, Lakhs (e.g. Rs. 17.73 Lakh Cr, Rs. 1,250.00 Cr).
    For USD/others: T, B, M, K (e.g. $3.15T, $500.00B, $45.20M).
    """
    if amount is None:
        return None
    curr = (currency or "").strip().upper()
    if curr in ("INR", "₹", "RS", "RUPEES"):
        prefix = "Rs. "
        abs_val = abs(amount)
        if abs_val >= 1e12:
            return f"{prefix}{amount / 1e12:.2f} Lakh Cr"
        elif abs_val >= 1e7:
            return f"{prefix}{amount / 1e7:,.2f} Cr"
        elif abs_val >= 1e5:
            return f"{prefix}{amount / 1e5:,.2f} Lakhs"
        else:
            return f"{prefix}{amount:,.2f}"
    else:
        prefix = "$" if curr in ("USD", "$", "") else f"{curr} "
        abs_val = abs(amount)
        if abs_val >= 1e12:
            return f"{prefix}{amount / 1e12:.2f}T"
        elif abs_val >= 1e9:
            return f"{prefix}{amount / 1e9:.2f}B"
        elif abs_val >= 1e6:
            return f"{prefix}{amount / 1e6:.2f}M"
        elif abs_val >= 1e3:
            return f"{prefix}{amount / 1e3:.2f}K"
        else:
            return f"{prefix}{amount:,.2f}"


def format_number_amount(amount: Optional[float], decimals: int = 2) -> Optional[str]:
    """Format number with thousands separators and fixed decimal precision (e.g. 2456.122 -> '2,456.12')."""
    if amount is None:
        return None
    return f"{amount:,.{decimals}f}"


def format_percent(val: Optional[float], include_sign: bool = False, decimals: int = 2) -> Optional[str]:
    """Format decimal fraction or percentage into string with % sign (e.g. 0.40389 -> '40.39%')."""
    if val is None:
        return None
    if abs(val) <= 1.0:
        pct = val * 100.0
    else:
        pct = val
    if include_sign:
        return f"{pct:+.{decimals}f}%"
    return f"{pct:.{decimals}f}%"


class MarketMetrics(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    currency: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None

    # --- Price & basic technicals ---
    current_price: Optional[float] = None
    current_price_formatted: Optional[str] = None
    fifty_day_ma: Optional[float] = None
    fifty_day_ma_formatted: Optional[str] = None
    two_hundred_day_ma: Optional[float] = None
    two_hundred_day_ma_formatted: Optional[str] = None

    # --- Extended technicals (computed from price history) ---
    rsi_14: Optional[float] = None                 # 14-day RSI
    macd_line: Optional[float] = None              # MACD line (12-26 EMA diff)
    macd_signal: Optional[float] = None            # Signal line (9-day EMA of MACD)
    macd_histogram: Optional[float] = None         # MACD - Signal
    volume_20d_avg: Optional[float] = None         # 20-day average daily volume
    volume_trend: Optional[str] = None             # "rising" | "falling" | "flat"
    support_level: Optional[float] = None          # derived from outlook-window price low
    resistance_level: Optional[float] = None       # derived from outlook-window price high

    # --- Valuation multiples ---
    market_cap: Optional[float] = None
    market_cap_formatted: Optional[str] = None # e.g. "Rs. 17.73 Lakh Cr" or "$3.12T"
    pe_ratio: Optional[float] = None          # trailing P/E
    pe_ratio_formatted: Optional[str] = None   # e.g. "17.15"
    forward_pe: Optional[float] = None        # forward P/E
    forward_pe_formatted: Optional[str] = None # e.g. "14.54"
    pb_ratio: Optional[float] = None          # price-to-book
    pb_ratio_formatted: Optional[str] = None   # e.g. "7.79"
    ps_ratio: Optional[float] = None          # price-to-sales (TTM)
    ps_ratio_formatted: Optional[str] = None   # e.g. "3.10"
    ev_ebitda: Optional[float] = None         # EV / EBITDA
    ev_ebitda_formatted: Optional[str] = None  # e.g. "11.40"
    dividend_yield: Optional[float] = None    # as a decimal, e.g. 0.012 = 1.2%
    dividend_yield_formatted: Optional[str] = None # e.g. "2.75%"

    # --- Earnings & revenue ---
    eps_ttm: Optional[float] = None           # EPS trailing twelve months
    eps_ttm_formatted: Optional[str] = None   # e.g. "137.65"
    revenue_ttm: Optional[float] = None       # total revenue TTM
    revenue_ttm_formatted: Optional[str] = None # e.g. "Rs. 9.50 Lakh Cr" or "$380.50B"
    gross_margin: Optional[float] = None      # gross margin as decimal
    gross_margin_formatted: Optional[str] = None # e.g. "40.39%"
    operating_margin: Optional[float] = None  # operating margin as decimal
    operating_margin_formatted: Optional[str] = None # e.g. "23.96%"

    # --- Extended fundamentals (from yfinance .info) ---
    debt_to_equity: Optional[float] = None    # total debt / total equity
    debt_to_equity_formatted: Optional[str] = None # e.g. "10.21"
    roe: Optional[float] = None               # return on equity (decimal)
    roe_formatted: Optional[str] = None       # e.g. "47.74%"
    roce: Optional[float] = None              # return on capital employed (decimal)
    roce_formatted: Optional[str] = None      # e.g. "54.93%"

    # --- Analyst ratings (from yfinance .info / recommendations / price targets) ---
    analyst_buy_count: Optional[int] = None
    analyst_hold_count: Optional[int] = None
    analyst_sell_count: Optional[int] = None
    analyst_target_mean: Optional[float] = None
    analyst_target_mean_formatted: Optional[str] = None # e.g. "2,456.12"
    analyst_target_high: Optional[float] = None
    analyst_target_high_formatted: Optional[str] = None # e.g. "3,480.00"
    analyst_target_low: Optional[float] = None
    analyst_target_low_formatted: Optional[str] = None  # e.g. "1,800.00"
    analyst_recommendation: Optional[str] = None  # e.g. "buy", "hold", "sell"

    # --- Ownership / holdings (from yfinance .major_holders) ---
    promoter_holding_pct: Optional[float] = None   # % held by promoters/insiders
    promoter_holding_pct_formatted: Optional[str] = None # e.g. "71.80%"
    fii_holding_pct: Optional[float] = None        # % held by foreign institutional investors
    institutional_holding_pct_formatted: Optional[str] = None # e.g. "17.45%"
    dii_holding_pct: Optional[float] = None        # % held by domestic institutional investors
    public_holding_pct: Optional[float] = None     # residual public float %
    public_holding_pct_formatted: Optional[str] = None # e.g. "10.75%"

    # --- Quarterly financials (last 4 quarters, computed in finance_tools) ---
    quarterly_financials: list[QuarterlyDataPoint] = Field(default_factory=list)

    # --- Configurable outlook window ---
    outlook_months: int = Field(default=6, description="Number of months the price window covers.")
    outlook_high: Optional[float] = None
    outlook_high_formatted: Optional[str] = None
    outlook_low: Optional[float] = None
    outlook_low_formatted: Optional[str] = None
    outlook_price_trend: list[PricePoint] = Field(default_factory=list)
    custom_metrics: dict[str, Any] = Field(
        default_factory=dict,
        description="Ad-hoc deterministic metrics computed via Python calculation sandbox.",
    )

    unavailable_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Field names yfinance could not supply. Surfaced explicitly so "
            "the Chief Editor states 'unretrievable' rather than the gap "
            "being silently papered over."
        ),
    )

    fetched_at: date = Field(default_factory=date.today)


# ---------------------------------------------------------------------------
# Stage 2: Research / Sentiment
# Agentic — produced via a bounded Gemini tool-calling loop over Tavily
# search in harness/agent_loop.py. This is the one stage where the LLM
# genuinely decides what to query and when it has enough information.
# ---------------------------------------------------------------------------

class SentimentLabel(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"


class CitedClaim(BaseModel):
    """A single claim that must trace back to a real, fetched source URL."""
    claim: str
    source_url: HttpUrl

    @field_validator("claim")
    @classmethod
    def claim_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("claim text cannot be empty")
        return v.strip()


class SentimentFindings(BaseModel):
    overall_sentiment: SentimentLabel
    sentiment_summary: str
    key_catalysts: list[CitedClaim] = Field(default_factory=list)
    key_risks: list[CitedClaim] = Field(default_factory=list)
    queries_used: list[str] = Field(
        default_factory=list,
        description="Search queries the agent actually issued — kept for auditability/debugging.",
    )
    extraction_failed: bool = Field(
        default=False,
        description="True if automated sentiment extraction encountered an error during parsing/synthesis.",
    )


# ---------------------------------------------------------------------------
# Stage 3: AML / ABC Screening (Layer 2)
# Deterministic loop — iterates over known entities and known free public
# data sources. Not truly agentic (no LLM decides what source to query next);
# the source list is fixed. A bounded Tavily search sub-loop handles adverse
# media and regulatory press releases where no structured API exists.
# ---------------------------------------------------------------------------

class AMLSeverity(str, Enum):
    NONE     = "None"
    WATCH    = "Watch"
    ELEVATED = "Elevated"
    HIGH     = "High"


class AMLFinding(BaseModel):
    """One screening hit — or an explicit 'no adverse finding' record."""
    entity_screened: str
    source_name: str          # e.g. "OFAC SDN List", "OpenSanctions", "SEC EDGAR FCPA"
    finding_summary: str      # plain-English description of what was (or wasn't) found
    severity: AMLSeverity
    source_url: str           # direct link to the result or source index


class AMLScreeningResult(BaseModel):
    entities_screened: list[str] = Field(
        description="All entity names that were run through the screening sources."
    )
    findings: list[AMLFinding] = Field(default_factory=list)
    screened_at: date = Field(default_factory=date.today)
    disclaimer: str = Field(
        default=(
            "This AML/ABC screening is an automated first-pass search of publicly available "
            "databases and is provided for informational purposes only. It does not constitute "
            "a legal or regulatory determination, does not represent AML/ABC clearance, and "
            "must not be used as a substitute for professional compliance due diligence. "
            "Absence of findings in this report does not certify that no adverse information exists."
        )
    )


# ---------------------------------------------------------------------------
# Ticker resolution
# ---------------------------------------------------------------------------

class TickerResolution(BaseModel):
    query: str
    resolved_ticker: Optional[str] = None
    confidence: float = 0.0
    method: str  # "static_map" | "yfinance_search" | "unresolved"


# ---------------------------------------------------------------------------
# Stage 4: Master Agentic Loop & Orchestrator Schemas
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    DONE = "done"
    FAILED = "failed"


class ToolCallRecord(BaseModel):
    turn: int
    tool_name: str
    arguments: dict
    result_summary: str          # short, for the trace log — not the raw payload
    ok: bool
    error: Optional[str] = None
    reasoning_text: Optional[str] = None


class ClarificationRequest(BaseModel):
    question: str
    options: list[str]           # e.g. ["Tata Motors (TATAMOTORS.NS)", "TCS (TCS.NS)", ...]


class ValidationResult(BaseModel):
    satisfied: bool
    missing: list[str]           # data categories still needed for this report_type
    contradictions: list[str]    # e.g. "AML found a sanctions hit but sentiment is unambiguously bullish"
    notes: str


class CustomMetricResult(BaseModel):
    metric_name: str
    value: Optional[float] = None
    raw_value: Optional[float] = None
    formatted_value: Optional[str] = None
    formatted: Optional[str] = None
    expression: str
    chronological_valid: bool = True
    status: str = "ok"          # "ok" | "error"
    notes: Optional[str] = None
    reason: Optional[str] = None


class SectionSpec(BaseModel):
    key: str                     # e.g. "financial_highlights", "demerger_sustainability"
    include: bool = True
    emphasis: Optional[str] = "" # short directive to the Chief Editor — what leads, what's a footnote
    order: int = 1
    title: Optional[str] = None  # optional custom heading text e.g. "## Post-Demerger Margin Sustainability"
    instruction: Optional[str] = None # optional tailored prompt instruction for Chief Editor


class ReportSpec(BaseModel):
    sections: list[SectionSpec]
    rationale: str               # why this shape — goes in the trace log, not the report itself
    editorial_goal: Optional[str] = None
    report_spec_source: str = "unknown"
    section_validation_errors: list[str] = Field(default_factory=list)


class RunTelemetry(BaseModel):
    gemini_calls: int = 0
    tavily_calls: int = 0
    tavily_calls_budget: int = 5
    wall_clock_seconds: float = 0.0
    extraction_failed: bool = False


class AgentState(BaseModel):
    user_query: str
    status: AgentStatus = AgentStatus.RUNNING
    report_type: ReportType = ReportType.GENERAL
    editorial_goal: Optional[str] = None
    run_aml: bool = False
    company_reference: Optional[str] = None
    candidate_entities: list[dict] = Field(default_factory=list)     # from resolve_entity, before disambiguation
    ticker: Optional[str] = None
    company_name: Optional[str] = None
    market_data: dict = Field(default_factory=dict)                  # incrementally filled by granular fetch tools
    custom_metrics: dict[str, Any] = Field(default_factory=dict)    # computed via calculation sandbox
    sentiment_findings: Optional[SentimentFindings] = None
    aml_result: Optional[AMLScreeningResult] = None
    report_spec: Optional[ReportSpec] = None
    pending_clarification: Optional[ClarificationRequest] = None
    tool_log: list[ToolCallRecord] = Field(default_factory=list)
    telemetry: RunTelemetry = Field(default_factory=RunTelemetry)
    turn: int = 0
    max_turns: int = 20


# ---------------------------------------------------------------------------
# Final assembled report
# ---------------------------------------------------------------------------

class FinalReport(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    report_type: ReportType = ReportType.GENERAL
    editorial_goal: Optional[str] = None
    generated_at: date = Field(default_factory=date.today)
    markdown_body: str  # the Chief Editor's compiled Markdown
    market_metrics: MarketMetrics
    sentiment_findings: SentimentFindings
    aml_result: Optional[AMLScreeningResult] = None  # populated only when --aml flag is set
    report_spec: Optional[ReportSpec] = None
    telemetry: Optional[RunTelemetry] = None
    kpi_cards: list[dict[str, str]] = Field(default_factory=list)

