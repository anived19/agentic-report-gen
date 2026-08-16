"""
AML/ABC Screening Orchestrator.

This module runs the Layer 2 compliance screening pipeline. It is a
bounded deterministic loop — not truly agentic. The source list is fixed;
what varies is the set of entities screened (derived from the company name
and a brief Tavily search for directors/promoters).

Two sub-phases:
  Phase 1 — Structured source screening (deterministic):
             For each entity, call each of the 6 structured sources
             (OFAC, OpenSanctions, World Bank, UN, EU, SEC EDGAR) and
             collect AMLFinding objects.

  Phase 2 — Adverse media search (semi-bounded Tavily loop):
             Run a small, focused set of Tavily queries against regulatory
             domain targets (SEBI, Enforcement Directorate, SFO/NCA, DOJ/SEC)
             to surface press releases and litigation mentions not captured
             by the structured lists. This is the one sub-phase where query
             selection involves some judgment — it uses the existing
             research agent pattern but with a narrow, AML-focused prompt.
"""
from __future__ import annotations

import logging
from datetime import date

from config import settings
from schemas import AMLFinding, AMLScreeningResult, AMLSeverity, MarketMetrics
from tools.aml_tools import (
    screen_eu_sanctions,
    screen_ofac_sdn,
    screen_opensanctions,
    screen_sec_fcpa,
    screen_un_sanctions,
    screen_world_bank_debarred,
)

logger = logging.getLogger(__name__)

# Structured-source screeners to run against each entity (in this order).
# Order matters for the output table: most authoritative sources first.
_STRUCTURED_SCREENERS = [
    screen_ofac_sdn,
    screen_opensanctions,
    screen_world_bank_debarred,
    screen_un_sanctions,
    screen_eu_sanctions,
    screen_sec_fcpa,
]


def _derive_entities(company_name: str, market_metrics: MarketMetrics) -> list[str]:
    """
    Build the list of entity names to screen.

    For this MVP, this is the company name itself (and any ticker-name
    variation). Director/promoter names require a data source with structured
    board-composition data (e.g. BSE/NSE filings or MCA) that isn't freely
    accessible via machine-readable API. The Tavily adverse-media phase will
    cover board-level searches via free-text queries instead.
    """
    entities = []
    if company_name:
        entities.append(company_name.strip())
    # Add ticker as a secondary identifier (avoids missing shell entities
    # that might be registered under the ticker-style name)
    if market_metrics.ticker and market_metrics.ticker not in entities:
        entities.append(market_metrics.ticker)
    return entities


def _run_adverse_media_phase(
    company_name: str,
    ticker: str,
    report_type_label: str = "general",
) -> list[AMLFinding]:
    """
    Run the adverse-media search sub-phase using Tavily + the AML screener agent.
    Returns a list of AMLFinding objects with severity derived from the findings.

    Uses the existing harness pattern: a bounded Gemini tool-calling loop with
    the AML screener system prompt, capped at a small turn budget.
    """
    try:
        from harness.aml_agent_loop import run_aml_adverse_media_agent
        return run_aml_adverse_media_agent(company_name=company_name, ticker=ticker)
    except Exception as exc:
        logger.warning("Adverse media phase failed: %s", exc)
        return [AMLFinding(
            entity_screened=company_name,
            source_name="Adverse Media (Tavily search)",
            finding_summary=f"Adverse media search could not be completed: {exc}. Manual review recommended.",
            severity=AMLSeverity.WATCH,
            source_url="",
        )]


def run_aml_screening(
    company_name: str,
    ticker: str,
    market_metrics: MarketMetrics,
) -> AMLScreeningResult:
    """
    Run the full AML/ABC screening pipeline for a company and return
    a validated AMLScreeningResult.

    Phase 1: structured source screening for each derived entity.
    Phase 2: Tavily adverse-media sweep for regulatory press releases.
    Phase 3: Collect jurisdictional context (TI CPI + FATF) based on the
             company's exchange/currency geography.
    """
    entities = _derive_entities(company_name, market_metrics)
    all_findings: list[AMLFinding] = []

    logger.info("AML screening: entities = %s", entities)

    # --- Phase 1: Structured source screening (Parallelized) ---
    import concurrent.futures

    # Build tasks: (entity, screener_fn) preserving intended output order
    tasks = []
    for entity in entities:
        for screener_fn in _STRUCTURED_SCREENERS:
            tasks.append((entity, screener_fn))

    def _execute_screener(item: tuple) -> AMLFinding:
        ent, fn = item
        try:
            finding = fn(ent)
            logger.info("    %s (%s) → %s", fn.__name__, ent, finding.severity.value)
            return finding
        except Exception as exc:
            logger.warning("    Screener %s failed for %r: %s", fn.__name__, ent, exc)
            return AMLFinding(
                entity_screened=ent,
                source_name=fn.__name__.replace("_", " ").title(),
                finding_summary=f"Screener error: {exc}. Manual check recommended.",
                severity=AMLSeverity.WATCH,
                source_url="",
            )

    logger.info("  Running %d structured screening tasks in parallel...", len(tasks))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks) or 1, 8)) as executor:
        structured_results = list(executor.map(_execute_screener, tasks))

    all_findings.extend(structured_results)

    # --- Phase 2: Adverse media ---
    logger.info("AML screening: running adverse media phase")
    media_findings = _run_adverse_media_phase(company_name, ticker)
    all_findings.extend(media_findings)

    # --- Phase 3: Jurisdictional context ---
    country_code = _infer_country_code(market_metrics)
    if country_code:
        from tools.aml_tools import get_fatf_risk, get_jurisdictional_risk
        all_findings.append(get_jurisdictional_risk(country_code))
        country_name = _COUNTRY_CODE_TO_NAME.get(country_code, country_code)
        all_findings.append(get_fatf_risk(country_name))

    return AMLScreeningResult(
        entities_screened=entities,
        findings=all_findings,
        screened_at=date.today(),
    )


def _infer_country_code(metrics: MarketMetrics) -> str | None:
    """Heuristically infer the company's primary jurisdiction from ticker suffix or currency."""
    ticker = metrics.ticker or ""
    if ticker.endswith(".NS") or ticker.endswith(".BO"):
        return "IN"
    if metrics.currency == "INR":
        return "IN"
    if metrics.currency == "USD":
        return "US"
    if metrics.currency == "GBP":
        return "GB"
    if metrics.currency == "SGD":
        return "SG"
    if metrics.currency == "AED":
        return "AE"
    return None


_COUNTRY_CODE_TO_NAME: dict[str, str] = {
    "IN": "India",
    "US": "United States",
    "GB": "United Kingdom",
    "CN": "China",
    "SG": "Singapore",
    "AE": "UAE",
    "MU": "Mauritius",
}
