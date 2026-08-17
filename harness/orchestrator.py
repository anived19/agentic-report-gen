"""
Master Orchestrator Harness — implements the single ReAct loop:
Reason -> Act -> Observe -> Validate -> Plan -> Finalize.

Replaces the fixed waterfall pipeline (steps 2-6) with one master loop.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from google import genai
from google.genai import types

from config import settings
from harness.agent_loop import _extract_structured_findings
from harness.gemini_retry import generate_with_retry
from harness.md_loader import SkillBundle, load_agent_prompt, load_skill
from harness.synthesis import render_aml_markdown, run_chief_editor
from schemas import (
    AgentState,
    AgentStatus,
    AMLFinding,
    AMLScreeningResult,
    AMLSeverity,
    ClarificationRequest,
    FinalReport,
    MarketMetrics,
    ReportSpec,
    ReportType,
    RunTelemetry,
    SectionSpec,
    SentimentFindings,
    SentimentLabel,
    ToolCallRecord,
    ValidationResult,
)
from tools.finance_tools import assemble_market_metrics
from tools.ticker_resolver import resolve_entity

logger = logging.getLogger(__name__)

_ORCHESTRATOR_CONFIG_PATH = Path("orchestrator_config.yaml")

def _load_orchestrator_config() -> dict[str, Any]:
    if _ORCHESTRATOR_CONFIG_PATH.exists():
        try:
            with open(_ORCHESTRATOR_CONFIG_PATH, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
                # Normalize keys to uppercase
                return {str(k).upper(): v for k, v in raw.items()}
        except Exception as exc:
            logger.warning("Could not load orchestrator_config.yaml: %s", exc)
    return {}

_ORCHESTRATOR_CONFIG = _load_orchestrator_config()

# 12 RPM target -> 60.0 / 12.0 = 5.0 seconds pacing interval
_GEMINI_PACING_INTERVAL = 5.0
_last_gemini_call_timestamp = 0.0


def _pace_gemini_call() -> None:
    """Enforce the 12 RPM pacing target headroom before issuing Gemini calls."""
    global _last_gemini_call_timestamp
    elapsed = time.time() - _last_gemini_call_timestamp
    if elapsed < _GEMINI_PACING_INTERVAL:
        sleep_dur = _GEMINI_PACING_INTERVAL - elapsed
        time.sleep(sleep_dur)
    _last_gemini_call_timestamp = time.time()


def ask_user(question: str, options: list[str]) -> str:
    """
    The only human-interaction point in the system.
    Pauses execution until the user selects an option.
    """
    # TODO: swap for async pause when moving off CLI
    print(f"\n{question}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        match = [o for o in options if raw.lower() in o.lower()]
        if len(match) == 1:
            return match[0]
        print("Didn't catch that — type the number or the name.")


# Dummy stubs to allow md_loader to resolve dotted callables for loop-internal tools
def validate_data(*args, **kwargs) -> dict:
    return {"status": "checked"}

def plan_report_format(*args, **kwargs) -> dict:
    return {"status": "planned"}

def finalize_report(*args, **kwargs) -> dict:
    return {"status": "finalized"}

def compute_custom_financial_metric(*args, **kwargs) -> dict:
    return {"status": "computed"}

def reflect_on_progress(*args, **kwargs) -> dict:
    return {"status": "reflected"}


class MasterOrchestrator:
    def __init__(
        self,
        user_query: str,
        initial_company_ref: Optional[str] = None,
        report_type: ReportType = ReportType.GENERAL,
        run_aml: bool = False,
        editorial_goal: Optional[str] = None,
        interactive_fn: Optional[Callable[[str, list[str]], str]] = None,
    ):
        self.state = AgentState(
            user_query=user_query,
            company_reference=initial_company_ref,
            report_type=report_type,
            editorial_goal=editorial_goal,
            run_aml=run_aml,
            telemetry=RunTelemetry(),
        )
        self.interactive_fn = interactive_fn or ask_user
        self.skills: dict[str, SkillBundle] = {}
        self._load_all_skills()

        # Internal tracking & guardrails
        self.seen_urls: set[str] = set()
        self.seen_titles: set[str] = set()
        self.consecutive_empty_searches: int = 0
        self.search_queries_used: list[str] = []
        self.raw_search_contents: list[types.Content] = []
        self.cached_idempotent_calls: dict[str, Any] = {}
        self.category_attempts: dict[str, int] = {}
        self._format_retries: int = 0

    def _load_all_skills(self) -> None:
        skill_names = [
            "resolve_entity",
            "ask_user",
            "get_price_snapshot",
            "get_valuation_multiples",
            "get_fundamentals",
            "get_quarterly_financials",
            "get_technicals",
            "get_ownership",
            "compute_custom_financial_metric",
            "search_web_news",
            "run_structured_aml_sweep",
            "search_adverse_media",
            "reflect_on_progress",
            "validate_data",
            "plan_report_format",
            "finalize_report",
        ]
        for name in skill_names:
            try:
                self.skills[name] = load_skill(name)
            except Exception as exc:
                logger.warning("Could not load skill %s: %s", name, exc)

    def _execute_validate_data(self) -> ValidationResult:
        """
        Dynamic validation of AgentState sufficiency against editorial_goal and report_type.
        Enforces category retry cap (Refinement #5): if a category was attempted >= 2 times
        and returned unavailable data, it does NOT hard-block finalization.
        """
        rt_key = self.state.report_type.value.upper()
        profile = _ORCHESTRATOR_CONFIG.get(rt_key, _ORCHESTRATOR_CONFIG.get("GENERAL", {}))
        required = profile.get("required", ["price_snapshot"])
        min_searches = profile.get("min_news_searches", 1)

        goal_lower = (self.state.editorial_goal or self.state.user_query or "").lower()

        # Dynamic adjustments based on editorial goal
        if any(term in goal_lower for term in ("sentiment", "news", "mood", "catalyst", "headline")):
            min_searches = max(min_searches, 1)
        if any(term in goal_lower for term in ("valuation", "multiple", "intrinsic", "cheap", "fair value", "target")):
            if "valuation_multiples" not in required:
                required = list(required) + ["valuation_multiples"]

        missing = []
        md = self.state.market_data

        category_map = {
            "price_snapshot": ["current_price", "market_cap"],
            "valuation_multiples": ["pe_ratio", "pb_ratio", "ps_ratio"],
            "fundamentals": ["eps_ttm", "debt_to_equity", "roe"],
            "technicals": ["rsi_14", "macd_line"],
            "ownership": ["promoter_holding_pct", "fii_holding_pct"],
            "quarterly_financials": ["quarterly_financials"],
        }

        for req in required:
            # Check retry cap guardrail: if attempted >= 2 times, don't hard-block
            attempts = self.category_attempts.get(f"get_{req}", self.category_attempts.get(req, 0))
            if attempts >= 2:
                continue

            keys = category_map.get(req, [req])
            if req == "quarterly_financials":
                if not md.get("quarterly_financials"):
                    missing.append(req)
            elif not any(k in md for k in keys):
                missing.append(req)

        # News searches validation with budget and attempt checks
        news_attempts = len(self.search_queries_used)
        if (
            news_attempts < min_searches
            and self.state.telemetry.tavily_calls < self.state.telemetry.tavily_calls_budget
            and self.category_attempts.get("search_web_news", 0) < 2
            and self.consecutive_empty_searches < 2
        ):
            missing.append(f"news_searches (need at least {min_searches}, ran {news_attempts})")

        satisfied = len(missing) == 0
        contradictions = []

        # Consistency checks
        if self.state.aml_result and any(f.severity in (AMLSeverity.HIGH, AMLSeverity.ELEVATED) for f in self.state.aml_result.findings):
            if self.state.sentiment_findings and self.state.sentiment_findings.overall_sentiment == SentimentLabel.BULLISH:
                contradictions.append("AML screening flagged elevated/high risk hits but sentiment assessment is Bullish.")

        if md.get("pe_ratio") is not None and md.get("eps_ttm") is None:
            contradictions.append("P/E multiple is present but EPS is null/unavailable.")

        return ValidationResult(
            satisfied=satisfied,
            missing=missing,
            contradictions=contradictions,
            notes=f"Validation complete against profile for {rt_key} (editorial goal: {self.state.editorial_goal or 'unspecified'})",
        )

    def _execute_plan_report_format(self, args: dict[str, Any]) -> ReportSpec:
        """
        Process or auto-plan a ReportSpec for the Chief Editor.
        Enforces maximum 5 to 7 sections total (Refinement #4) and dynamically tailors
        the blueprint to the self-generated editorial_goal.
        """
        rationale = args.get("rationale", "")
        raw_sections = args.get("sections", [])
        sections = []
        section_validation_errors: list[str] = []

        if raw_sections:
            for idx, s in enumerate(raw_sections, 1):
                try:
                    sections.append(SectionSpec.model_validate(s))
                except Exception as exc:
                    logger.warning("Failed to validate section %d (%r): %s", idx, s, exc)
                    section_validation_errors.append(f"Section {idx} ({s}): {exc}")

        # Bounding sections: cap at maximum 7 active sections (Refinement #4)
        if sections:
            sections = sorted(sections, key=lambda x: x.order)[:7]

        # Determine source: model-supplied vs fallback ladder
        report_spec_source = "agent" if sections else "fallback"

        if not sections:
            # Generate adaptive section spec based on editorial_goal & report_type
            goal_lower = (self.state.editorial_goal or self.state.user_query or "").lower()
            rt = self.state.report_type

            if any(term in goal_lower for term in ("sentiment", "news", "momentum", "macro", "catalyst")):
                rationale = rationale or f"Sentiment & Catalyst focus: prioritizing market mood, headlines, and strategic risks for {self.state.editorial_goal or self.state.ticker}."
                sections = [
                    SectionSpec(key="executive_summary", include=True, order=1, emphasis="Highlight market sentiment, news catalysts, and near-term direction."),
                    SectionSpec(key="sentiment_news", include=True, order=2, emphasis="Lead with detailed catalysts and cited headline risks."),
                    SectionSpec(key="risk_factors", include=True, order=3, emphasis="Detail regulatory, competitive, and macro risks."),
                    SectionSpec(key="scenario_outlook", include=True, order=4, emphasis="Frame Bull/Base/Bear scenarios from sentiment and catalyst outlook."),
                ]
            elif any(term in goal_lower for term in ("valuation", "multiple", "demerger", "discount", "fair value", "target")):
                rationale = rationale or f"Valuation focus: prioritizing fundamental metrics, multiples, and intrinsic analyst targets for {self.state.editorial_goal or self.state.ticker}."
                sections = [
                    SectionSpec(key="executive_summary", include=True, order=1, emphasis="Focus on valuation summary and fair value vs market price."),
                    SectionSpec(key="financial_highlights", include=True, order=2, emphasis="Lead with market cap, price, and moving average baseline."),
                    SectionSpec(key="fundamentals_deep_dive", include=True, order=3, emphasis="Thorough analysis of EPS, ROE, ROCE, and quarterly growth."),
                    SectionSpec(key="valuation_analysis", include=True, order=4, emphasis="Lead with P/E, forward P/E, EV/EBITDA, and margin metrics."),
                    SectionSpec(key="scenario_outlook", include=True, order=5, emphasis="Frame outlook around fair value convergence scenarios."),
                ]
            elif rt == ReportType.EQUITY:
                rationale = rationale or "Comprehensive equity analysis across valuation, technicals, and sentiment."
                sections = [
                    SectionSpec(key="executive_summary", include=True, order=1, emphasis="Comprehensive overview balancing valuation, momentum, and outlook."),
                    SectionSpec(key="financial_highlights", include=True, order=2, emphasis="Baseline financial highlights and scale."),
                    SectionSpec(key="fundamentals_deep_dive", include=True, order=3, emphasis="EPS, ROE, and quarterly financials deep dive."),
                    SectionSpec(key="technicals", include=True, order=4, emphasis="RSI, MACD, and volume trend momentum analysis."),
                    SectionSpec(key="valuation_analysis", include=True, order=5, emphasis="Valuation multiples comparison."),
                    SectionSpec(key="sentiment_news", include=True, order=6, emphasis="Catalysts and risks summary."),
                    SectionSpec(key="scenario_outlook", include=True, order=7, emphasis="Comprehensive Bull/Base/Bear scenario breakdown."),
                ]
            elif rt == ReportType.SENTIMENT:
                rationale = rationale or "Sentiment focus: lead with momentum and news sentiment; market cap is secondary."
                sections = [
                    SectionSpec(key="executive_summary", include=True, order=1, emphasis="Highlight market sentiment and near-term news catalysts."),
                    SectionSpec(key="sentiment_news", include=True, order=2, emphasis="Lead with detailed catalysts and cited risks."),
                    SectionSpec(key="risk_factors", include=True, order=3, emphasis="Detail regulatory and macro risks."),
                    SectionSpec(key="scenario_outlook", include=True, order=4, emphasis="Frame Bull/Base/Bear scenarios from sentiment and catalyst outlook."),
                ]
            elif rt == ReportType.VALUATION:
                rationale = rationale or "Valuation focus: lead with financial metrics, multiples, and intrinsic analyst targets."
                sections = [
                    SectionSpec(key="executive_summary", include=True, order=1, emphasis="Focus on valuation summary and fair value vs market price."),
                    SectionSpec(key="financial_highlights", include=True, order=2, emphasis="Lead with market cap, price, and moving average baseline."),
                    SectionSpec(key="fundamentals_deep_dive", include=True, order=3, emphasis="Thorough analysis of EPS, ROE, ROCE, and quarterly growth."),
                    SectionSpec(key="valuation_analysis", include=True, order=4, emphasis="Lead with P/E, forward P/E, EV/EBITDA, and margin metrics."),
                    SectionSpec(key="scenario_outlook", include=True, order=5, emphasis="Frame outlook around fair value convergence scenarios."),
                ]
            else:
                rationale = rationale or f"Financial review for {self.state.editorial_goal or self.state.ticker}."
                sections = [
                    SectionSpec(key="executive_summary", include=True, order=1, emphasis="Standard executive summary."),
                    SectionSpec(key="financial_highlights", include=True, order=2, emphasis="Financial highlights."),
                    SectionSpec(key="sentiment_news", include=True, order=3, emphasis="Market sentiment and news."),
                    SectionSpec(key="risk_factors", include=True, order=4, emphasis="Key risk factors."),
                    SectionSpec(key="scenario_outlook", include=True, order=5, emphasis="Near-term outlook."),
                ]

        # Enforce hard cap at 7 sections (Refinement #4)
        sections = sections[:7]
        spec = ReportSpec(
            sections=sections,
            rationale=rationale,
            editorial_goal=self.state.editorial_goal,
            report_spec_source=report_spec_source,
            section_validation_errors=section_validation_errors,
        )
        self.state.report_spec = spec
        return spec

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> tuple[Any, str, bool, Optional[str]]:
        """
        Execute tool call with:
        - Idempotency caching
        - Tavily budget & diminishing returns checks
        - State updates
        - Category attempt tracking
        """
        self.category_attempts[tool_name] = self.category_attempts.get(tool_name, 0) + 1

        # Check idempotency
        idempotency_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        if idempotency_key in self.cached_idempotent_calls:
            cached_res = self.cached_idempotent_calls[idempotency_key]
            return cached_res, "Cached result returned (idempotent)", True, None

        if tool_name == "resolve_entity":
            query = args.get("query", self.state.company_reference or self.state.user_query)
            candidates = resolve_entity(query)
            self.state.candidate_entities = candidates

            if len(candidates) == 0:
                err = f"Could not resolve a ticker for '{query}'. Aborting — no report is generated on unresolved/guessed data."
                self.state.status = AgentStatus.FAILED
                return {"candidates": []}, err, False, err

            if len(candidates) == 1:
                self.state.ticker = candidates[0]["ticker"]
                self.state.company_name = candidates[0].get("name")
                summary = f"Resolved single ticker: {self.state.ticker} ({self.state.company_name})"
                self.cached_idempotent_calls[idempotency_key] = {"candidates": candidates}
                return {"candidates": candidates}, summary, True, None

            # >1 candidate -> prepare for ask_user
            summary = f"Found {len(candidates)} candidates: " + ", ".join(c["ticker"] for c in candidates)
            self.cached_idempotent_calls[idempotency_key] = {"candidates": candidates}
            return {"candidates": candidates}, summary, True, None

        elif tool_name == "ask_user":
            question = args.get("question", "Which company did you mean?")
            options = args.get("options", [])
            if not options and self.state.candidate_entities:
                options = [f"{c.get('name', '')} ({c.get('ticker', '')})" for c in self.state.candidate_entities]

            self.state.status = AgentStatus.AWAITING_USER
            self.state.pending_clarification = ClarificationRequest(question=question, options=options)

            # Call interactive handler
            selected = self.interactive_fn(question, options)
            self.state.status = AgentStatus.RUNNING
            self.state.pending_clarification = None

            # Extract chosen ticker from selection
            for c in self.state.candidate_entities:
                if c.get("ticker", "") in selected or c.get("name", "").lower() in selected.lower():
                    self.state.ticker = c["ticker"]
                    self.state.company_name = c.get("name")
                    break
            if not self.state.ticker:
                # Fallback extraction from string e.g. "TCS.NS"
                import re
                m = re.search(r"\(([^)]+)\)", selected)
                if m:
                    self.state.ticker = m.group(1).strip()
                else:
                    self.state.ticker = selected.strip()

            summary = f"User selected: {selected} (ticker: {self.state.ticker})"
            return {"selected": selected, "ticker": self.state.ticker}, summary, True, None

        elif tool_name in (
            "get_price_snapshot",
            "get_valuation_multiples",
            "get_fundamentals",
            "get_quarterly_financials",
            "get_technicals",
            "get_ownership",
        ):
            ticker = args.get("ticker") or self.state.ticker
            if not ticker:
                return {}, "No ticker available to fetch market data", False, "Missing ticker"

            skill = self.skills.get(tool_name)
            if not skill:
                return {}, f"Skill {tool_name} not found", False, "Skill not found"

            res = skill.function(ticker)
            if isinstance(res, dict):
                self.state.market_data.update(res)
                if res.get("company_name"):
                    self.state.company_name = res.get("company_name")
            elif isinstance(res, list) and tool_name == "get_quarterly_financials":
                self.state.market_data["quarterly_financials"] = res

            summary = f"Fetched {tool_name} for {ticker}"
            self.cached_idempotent_calls[idempotency_key] = res
            return res, summary, True, None

        elif tool_name == "compute_custom_financial_metric":
            expression = args.get("expression", "")
            context = args.get("context")
            ticker = args.get("ticker") or self.state.ticker
            metric_name = args.get("metric_name")

            from tools.finance_tools import compute_custom_financial_metric as calc_tool
            res = calc_tool(
                expression=expression,
                context=context,
                ticker=ticker,
                metric_name=metric_name,
            )

            m_name = res.get("metric_name", "custom_metric")
            self.state.custom_metrics[m_name] = res
            if "custom_metrics" not in self.state.market_data:
                self.state.market_data["custom_metrics"] = {}
            self.state.market_data["custom_metrics"][m_name] = res

            summary = f"Computed {m_name}: {res.get('formatted_value')} (status: {res.get('status')})"
            self.cached_idempotent_calls[idempotency_key] = res
            return res, summary, True, None

        elif tool_name == "search_web_news":
            # Budget check
            if self.state.telemetry.tavily_calls >= self.state.telemetry.tavily_calls_budget:
                return {"results": []}, "Tavily budget ceiling reached (5/5)", True, None

            # Diminishing returns check
            if self.consecutive_empty_searches >= 2:
                return {"results": []}, "Diminishing returns threshold met; search ceased", True, None

            query = args.get("query", "")
            ticker = args.get("ticker") or self.state.ticker or ""
            depth = args.get("depth", "basic")

            skill = self.skills.get("search_web_news")
            res = skill.function(query=query, ticker=ticker, depth=depth)
            
            # Update telemetry
            cost = 2 if depth == "advanced" else 1
            self.state.telemetry.tavily_calls += cost
            self.search_queries_used.append(query)

            # Check new findings
            new_items = 0
            for r in res:
                u = r.get("url", "")
                t = r.get("title", "")
                if u and u not in self.seen_urls:
                    self.seen_urls.add(u)
                    new_items += 1
                if t and t not in self.seen_titles:
                    self.seen_titles.add(t)

            if new_items == 0:
                self.consecutive_empty_searches += 1
            else:
                self.consecutive_empty_searches = 0

            summary = f"Retrieved {len(res)} results ({new_items} new) for {query!r}"
            self.cached_idempotent_calls[idempotency_key] = res
            return res, summary, True, None

        elif tool_name == "run_structured_aml_sweep":
            entity_name = args.get("entity_name") or self.state.company_name or self.state.company_reference or ""
            ticker = args.get("ticker") or self.state.ticker or ""
            skill = self.skills.get("run_structured_aml_sweep")
            raw_findings = skill.function(entity_name=entity_name, ticker=ticker)
            findings_objs = [AMLFinding.model_validate(f) for f in raw_findings]

            entities_screened = [entity_name]
            if ticker and ticker not in entities_screened:
                entities_screened.append(ticker)

            if not self.state.aml_result:
                self.state.aml_result = AMLScreeningResult(
                    entities_screened=entities_screened,
                    findings=findings_objs,
                    screened_at=date.today(),
                )
            else:
                self.state.aml_result.findings.extend(findings_objs)

            summary = f"Completed AML sweep: {len(findings_objs)} findings across structured sources"
            self.cached_idempotent_calls[idempotency_key] = raw_findings
            return raw_findings, summary, True, None

        elif tool_name == "search_adverse_media":
            if self.state.telemetry.tavily_calls >= self.state.telemetry.tavily_calls_budget:
                return {"findings": []}, "Tavily budget ceiling reached (5/5)", True, None

            entity_name = args.get("entity_name") or self.state.company_name or self.state.company_reference or ""
            focus = args.get("focus", "")
            depth = args.get("depth", "basic")

            skill = self.skills.get("search_adverse_media")
            raw_findings = skill.function(entity_name=entity_name, focus=focus, depth=depth)
            
            cost = 2 if depth == "advanced" else 1
            self.state.telemetry.tavily_calls += cost

            findings_objs = [AMLFinding.model_validate(f) for f in raw_findings]
            if not self.state.aml_result:
                self.state.aml_result = AMLScreeningResult(
                    entities_screened=[entity_name],
                    findings=findings_objs,
                    screened_at=date.today(),
                )
            else:
                self.state.aml_result.findings.extend(findings_objs)

            summary = f"Adverse media search ({focus or 'broad'}): {len(findings_objs)} findings"
            self.cached_idempotent_calls[idempotency_key] = raw_findings
            return raw_findings, summary, True, None

        elif tool_name == "validate_data":
            v = self._execute_validate_data()
            summary = f"Validation: satisfied={v.satisfied}, missing={v.missing}"
            return v.model_dump(), summary, True, None

        elif tool_name == "plan_report_format":
            raw_sections = args.get("sections", [])
            valid_sections = []
            validation_errors = []
            if raw_sections:
                for idx, s in enumerate(raw_sections, 1):
                    try:
                        valid_sections.append(SectionSpec.model_validate(s))
                    except Exception as exc:
                        logger.warning("Failed to validate section %d (%r): %s", idx, s, exc)
                        validation_errors.append(f"Section {idx} ({s}): {exc}")

            if (not raw_sections or not valid_sections) and self._format_retries < 2:
                self._format_retries += 1
                if not raw_sections:
                    err = (
                        "Error: 'sections' cannot be empty. You must explicitly design the "
                        "report sections and emphasize specific data points you retrieved "
                        "(e.g. the exact price movement, AML flags, or custom YoY growth metrics). "
                        "Retry plan_report_format with a fully populated 'sections' list."
                    )
                else:
                    err = (
                        f"Error: Received {len(raw_sections)} section(s), but none were valid SectionSpec objects. "
                        f"Validation errors: {'; '.join(validation_errors)}. "
                        "Each section requires 'key' (e.g. 'executive_summary', 'financial_highlights', "
                        "'fundamentals_deep_dive', 'technicals', 'valuation_analysis', 'sentiment_news', "
                        "'risk_factors', 'scenario_outlook', or custom snake_case), 'include' (bool, default True), "
                        "'order' (int), and 'emphasis' (str). "
                        "Retry plan_report_format with valid section specifications."
                    )
                return {}, err, False, err

            spec = self._execute_plan_report_format(args)
            summary = f"Planned report format ({spec.report_spec_source}): {len(spec.sections)} sections. Rationale: {spec.rationale[:60]}..."
            return spec.model_dump(), summary, True, None

        elif tool_name == "reflect_on_progress":
            gathered_summary = args.get("gathered_summary", "")
            still_needed = args.get("still_needed", [])
            next_action_rationale = args.get("next_action_rationale", "")
            summary = (
                f"Reflection recorded: {len(still_needed)} gap(s) noted"
                + (f" — {still_needed}" if still_needed else " — none, ready to finalize")
            )
            return {"acknowledged": True}, summary, True, None

        elif tool_name == "finalize_report":
            v = self._execute_validate_data()
            if not v.satisfied:
                err = f"Cannot finalize: missing required categories {v.missing}"
                return {"ok": False, "missing": v.missing}, err, False, err
            if self.category_attempts.get("reflect_on_progress", 0) == 0:
                err = (
                    "Cannot finalize: call reflect_on_progress first, summarizing "
                    "what was gathered and confirming nothing further is needed "
                    "relative to the editorial goal."
                )
                return {"ok": False}, err, False, err
            if not self.state.report_spec:
                self._execute_plan_report_format({})
            self.state.status = AgentStatus.DONE
            summary = "Report finalized and validated successfully"
            return {"ok": True}, summary, True, None

        else:
            err = f"Unknown tool: {tool_name}"
            return {}, err, False, err

    def run_loop(self) -> tuple[AgentState, FinalReport]:
        """Execute the master agentic orchestrator loop."""
        start_time = time.time()
        client = genai.Client(api_key=settings.gemini_api_key)
        system_prompt = load_agent_prompt("orchestrator")

        # Expose all available skills to Gemini
        declarations = [s.declaration for s in self.skills.values()]
        tools = [types.Tool(function_declarations=declarations)]
        loop_config = types.GenerateContentConfig(system_instruction=system_prompt, tools=tools)

        user_initial_text = (
            f"User request: {self.state.user_query}\n"
            f"Detected Prior Company Reference: {self.state.company_reference or 'Unspecified'}\n"
            f"Detected Prior Report Type: {self.state.report_type.value}\n"
            f"Editorial Goal / Framing: {self.state.editorial_goal or 'Standard Financial Assessment'}\n"
            f"AML Screening Enabled: {self.state.run_aml}\n\n"
            f"Instructions:\n"
            f"1. Begin by calling resolve_entity with the company/group reference.\n"
            f"2. If resolve_entity returns MORE THAN ONE candidate (e.g. for group names like 'Tata', 'Adani', 'Reliance', 'Mahindra', 'Bajaj'), you MUST immediately call ask_user. Do NOT guess a specific company.\n"
            f"3. Fetch required market data categories for the {self.state.report_type.value} report type.\n"
            f"4. If ad-hoc calculations (CAGR, FCF Yield, custom spreads, margins) are required to satisfy the editorial goal, call compute_custom_financial_metric.\n"
            f"5. Run news and adverse media searches within the shared 5-call Tavily budget.\n"
            f"6. Call reflect_on_progress(), then validate_data(), then plan_report_format(), then finalize_report()."
        )

        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=user_initial_text)])
        ]

        while self.state.turn < self.state.max_turns and self.state.status == AgentStatus.RUNNING:
            self.state.turn += 1
            logger.info("--- Orchestrator Turn %d/%d (status: %s) ---", self.state.turn, self.state.max_turns, self.state.status.value)

            _pace_gemini_call()
            self.state.telemetry.gemini_calls += 1

            try:
                response = generate_with_retry(
                    client,
                    model=settings.gemini_model,
                    contents=contents,
                    config=loop_config,
                )
            except Exception as exc:
                logger.error("Gemini call failed during turn %d: %s", self.state.turn, exc)
                self.state.status = AgentStatus.FAILED
                break

            if response.candidates and response.candidates[0].content:
                contents.append(response.candidates[0].content)

            # Extract any non-function-call text parts (reasoning emitted by Gemini)
            reasoning_text: Optional[str] = None
            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                reasoning_text = " ".join(
                    p.text for p in response.candidates[0].content.parts
                    if getattr(p, "text", None)
                ).strip() or None

            if reasoning_text:
                logger.info("[thought turn %d] %s", self.state.turn, reasoning_text)

            calls = response.function_calls or []
            if not calls:
                # If model stopped calling tools without finalizing, check validation
                v = self._execute_validate_data()
                if v.satisfied:
                    if not self.state.report_spec:
                        self._execute_plan_report_format({})
                    self.state.status = AgentStatus.DONE
                    break
                else:
                    # Feed back validation requirements to continue the loop
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=f"Validation incomplete. Missing categories: {v.missing}. Fetch the remaining required data.")]
                    ))
                    continue

            function_response_parts = []
            for call in calls:
                args = call.args or {}
                result_payload, summary, ok, error = self._dispatch_tool(call.name, args)

                record = ToolCallRecord(
                    turn=self.state.turn,
                    tool_name=call.name,
                    arguments=args,
                    result_summary=summary,
                    ok=ok,
                    error=error,
                    reasoning_text=reasoning_text,
                )
                self.state.tool_log.append(record)
                logger.info("[act] %s(%s) -> ok=%s: %s", call.name, args, ok, summary)

                if call.name == "search_web_news" and ok:
                    # Accumulate for Phase B extraction
                    self.raw_search_contents.extend([
                        types.Content(role="model", parts=[types.Part.from_function_call(name=call.name, args=args)]),
                        types.Content(role="user", parts=[types.Part.from_function_response(name=call.name, response={"result": result_payload})])
                    ])

                if self.state.status == AgentStatus.FAILED:
                    logger.error("Loop failed during tool %s: %s", call.name, error)
                    break

                function_response_parts.append(
                    types.Part.from_function_response(name=call.name, response={"result": result_payload} if ok else {"error": error})
                )

            if self.state.status == AgentStatus.FAILED:
                break

            contents.append(types.Content(role="user", parts=function_response_parts))

        # Check turn limit exhaustion
        if self.state.turn >= self.state.max_turns and self.state.status == AgentStatus.RUNNING:
            v = self._execute_validate_data()
            if not v.satisfied:
                self.state.status = AgentStatus.FAILED
                logger.error("Orchestrator exhausted max_turns (%d) with unsatisfied categories: %s", self.state.max_turns, v.missing)
            else:
                if not self.state.report_spec:
                    self._execute_plan_report_format({})
                self.state.status = AgentStatus.DONE

        self.state.telemetry.wall_clock_seconds = round(time.time() - start_time, 2)

        # Assemble final models
        if self.state.status == AgentStatus.FAILED:
            self._dump_trace()
            raise RuntimeError(f"Orchestrator loop failed for query {self.state.user_query!r}. Check tool log.")

        # 1. Assemble MarketMetrics
        ticker = self.state.ticker or "UNKNOWN"
        market_metrics = assemble_market_metrics(ticker, self.state.market_data)

        # 2. Extract SentimentFindings if news searches occurred
        if not self.state.sentiment_findings:
            if self.search_queries_used:
                logger.info("Extracting structured SentimentFindings from accumulated searches...")
                _pace_gemini_call()
                self.state.telemetry.gemini_calls += 1
                try:
                    self.state.sentiment_findings = _extract_structured_findings(
                        client,
                        self.raw_search_contents,
                        self.search_queries_used,
                    )
                except Exception as exc:
                    logger.warning("Sentiment findings extraction failed: %s — recording honest failure state", exc)
                    self.state.telemetry.extraction_failed = True
                    self.state.sentiment_findings = SentimentFindings(
                        overall_sentiment=SentimentLabel.NEUTRAL,
                        sentiment_summary="Automated sentiment extraction did not complete successfully for this run; no catalysts or risks could be structured from search results.",
                        queries_used=self.search_queries_used,
                        extraction_failed=True,
                    )
            else:
                self.state.sentiment_findings = SentimentFindings(
                    overall_sentiment=SentimentLabel.NEUTRAL,
                    sentiment_summary="No external sentiment searches required for this technical/valuation run.",
                    queries_used=[],
                    extraction_failed=False,
                )

        # 3. Chief Editor synthesis
        _pace_gemini_call()
        self.state.telemetry.gemini_calls += 1
        markdown_body = run_chief_editor(
            market_metrics=market_metrics,
            sentiment_findings=self.state.sentiment_findings,
            report_type=self.state.report_type,
            report_spec=self.state.report_spec,
            editorial_goal=self.state.editorial_goal,
            aml_result=self.state.aml_result if self.state.run_aml else None,
        )

        # 4. If AML enabled and AML results exist, append deterministic table
        if self.state.run_aml and self.state.aml_result:
            aml_md = render_aml_markdown(self.state.aml_result)
            markdown_body = markdown_body + "\n\n" + aml_md

        # 5. Assemble KPI summary cards for adaptive document rendering
        kpi_cards: list[dict[str, str]] = []
        if market_metrics.current_price_formatted:
            kpi_cards.append({"label": "Current Price", "value": market_metrics.current_price_formatted, "note": "Market close"})
        if market_metrics.market_cap_formatted:
            kpi_cards.append({"label": "Market Cap", "value": market_metrics.market_cap_formatted, "note": "Scale"})
        if market_metrics.pe_ratio_formatted:
            kpi_cards.append({"label": "P/E Ratio", "value": market_metrics.pe_ratio_formatted, "note": "TTM multiple"})
        if market_metrics.roe_formatted:
            kpi_cards.append({"label": "Return on Equity", "value": market_metrics.roe_formatted, "note": "Profitability"})
        for cm_name, cm_val in self.state.custom_metrics.items():
            if isinstance(cm_val, dict) and cm_val.get("formatted_value") and cm_val.get("status") == "ok":
                kpi_cards.append({
                    "label": cm_name.replace("_", " ").title(),
                    "value": str(cm_val["formatted_value"]),
                    "note": "Custom Sandbox Metric",
                })

        final_report = FinalReport(
            ticker=ticker,
            company_name=self.state.company_name or market_metrics.company_name,
            report_type=self.state.report_type,
            editorial_goal=self.state.editorial_goal,
            markdown_body=markdown_body,
            market_metrics=market_metrics,
            sentiment_findings=self.state.sentiment_findings,
            aml_result=self.state.aml_result,
            report_spec=self.state.report_spec,
            telemetry=self.state.telemetry,
            kpi_cards=kpi_cards[:6],
        )

        self._dump_trace()
        return self.state, final_report

    def _dump_trace(self) -> None:
        """Write execution trace to outputs/TICKER_DATE_trace.json."""
        try:
            settings.output_dir.mkdir(parents=True, exist_ok=True)
            ticker_slug = (self.state.ticker or "UNRESOLVED").replace(".", "_").replace("/", "_")
            date_slug = date.today().isoformat()
            trace_path = settings.output_dir / f"{ticker_slug}_{date_slug}_trace.json"

            trace_data = {
                "user_query": self.state.user_query,
                "ticker": self.state.ticker,
                "company_name": self.state.company_name,
                "report_type": self.state.report_type.value,
                "editorial_goal": self.state.editorial_goal,
                "run_aml": self.state.run_aml,
                "status": self.state.status.value,
                "turn": self.state.turn,
                "telemetry": self.state.telemetry.model_dump(),
                "report_spec": self.state.report_spec.model_dump() if self.state.report_spec else None,
                "custom_metrics": self.state.custom_metrics,
                "tool_log": [t.model_dump() for t in self.state.tool_log],
            }
            trace_path.write_text(json.dumps(trace_data, indent=2), encoding="utf-8")
            logger.info("Trace file written to %s", trace_path)
        except Exception as exc:
            logger.warning("Could not write trace file: %s", exc)


def run_orchestrator(
    user_query: str,
    initial_company_ref: Optional[str] = None,
    report_type: ReportType = ReportType.GENERAL,
    run_aml: bool = False,
    editorial_goal: Optional[str] = None,
    interactive_fn: Optional[Callable[[str, list[str]], str]] = None,
) -> tuple[AgentState, FinalReport]:
    """Convenience entry point for running the master orchestrator."""
    orchestrator = MasterOrchestrator(
        user_query=user_query,
        initial_company_ref=initial_company_ref,
        report_type=report_type,
        run_aml=run_aml,
        editorial_goal=editorial_goal,
        interactive_fn=interactive_fn,
    )
    return orchestrator.run_loop()
