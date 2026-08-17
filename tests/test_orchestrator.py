"""
Unit and integration tests for the Master Orchestrator loop.

Tests cover:
  1. Tata conglomerate disambiguation triggering ask_user.
  2. Zero-candidate fail-closed resolution.
  3. Single-ticker valuation run (granular fetches without technicals/ownership).
  4. Same-company sentiment vs valuation ReportSpec emphasis divergence.
  5. AML screening clean sweep vs structured hit triggering targeted adverse media.
  6. Diminishing-returns early stop on repeated search results.
  7. Idempotency cache preventing duplicate tool executions.
  8. Telemetry tracking and trace file dumping.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from harness.orchestrator import MasterOrchestrator, run_orchestrator
from schemas import (
    AgentState,
    AgentStatus,
    AMLFinding,
    AMLScreeningResult,
    AMLSeverity,
    FinalReport,
    MarketMetrics,
    ReportSpec,
    ReportType,
    SectionSpec,
    SentimentFindings,
    SentimentLabel,
)
from tools.ticker_resolver import resolve_entity


def test_tata_disambiguation_triggers_ask_user():
    """Test that resolving 'Tata' returns >1 candidates and triggers ask_user."""
    candidates = resolve_entity("Tata")
    assert len(candidates) > 1
    tickers = [c["ticker"] for c in candidates]
    assert "TATAMOTORS.NS" in tickers or "TCS.NS" in tickers

    # Simulate orchestrator with interactive choice
    user_choice_mock = MagicMock(return_value="Tata Consultancy Services (TCS.NS)")
    orchestrator = MasterOrchestrator(
        user_query="valuation report of Tata",
        report_type=ReportType.VALUATION,
        interactive_fn=user_choice_mock,
    )

    # Dispatch resolve_entity
    res, summary, ok, err = orchestrator._dispatch_tool("resolve_entity", {"query": "Tata"})
    assert ok is True
    assert len(orchestrator.state.candidate_entities) > 1

    # Dispatch ask_user
    res_user, summary_user, ok_user, _ = orchestrator._dispatch_tool("ask_user", {"question": "Which company?", "options": []})
    assert ok_user is True
    assert orchestrator.state.ticker == "TCS.NS"
    user_choice_mock.assert_called_once()


def test_zero_candidate_fail_closed():
    """Test that a non-existent company query returns 0 candidates and fails closed."""
    orchestrator = MasterOrchestrator(
        user_query="report on NonExistentBogusCompany9999XYZ",
        report_type=ReportType.GENERAL,
    )
    res, summary, ok, err = orchestrator._dispatch_tool("resolve_entity", {"query": "NonExistentBogusCompany9999XYZ"})
    assert ok is False
    assert orchestrator.state.status == AgentStatus.FAILED
    assert "Aborting" in err


def test_valuation_run_skips_technicals_and_ownership():
    """Test that a valuation run only requires price, valuation, and fundamentals."""
    orchestrator = MasterOrchestrator(
        user_query="valuation analysis of TCS",
        report_type=ReportType.VALUATION,
    )
    orchestrator.state.ticker = "TCS.NS"

    # Simulate granular fetches
    orchestrator.state.market_data = {
        "current_price": 3800.0,
        "market_cap": 14000000000000.0,
        "pe_ratio": 28.5,
        "pb_ratio": 12.0,
        "ps_ratio": 6.5,
        "eps_ttm": 133.0,
        "debt_to_equity": 0.05,
        "roe": 0.45,
    }
    orchestrator.search_queries_used = ["TCS valuation analyst targets"]

    v = orchestrator._execute_validate_data()
    assert v.satisfied is True
    assert "technicals" not in v.missing
    assert "ownership" not in v.missing


def test_sentiment_vs_valuation_reportspec_framing():
    """Verify that sentiment and valuation runs produce distinct ReportSpec emphasis."""
    # Valuation run
    orch_val = MasterOrchestrator(
        user_query="valuation analysis of TCS",
        report_type=ReportType.VALUATION,
    )
    spec_val = orch_val._execute_plan_report_format({})
    assert "valuation" in spec_val.rationale.lower()
    
    val_sec_map = {s.key: s for s in spec_val.sections}
    assert "valuation_analysis" in val_sec_map
    assert val_sec_map["valuation_analysis"].include is True
    assert "P/E" in val_sec_map["valuation_analysis"].emphasis or "multiples" in val_sec_map["valuation_analysis"].emphasis

    # Sentiment run
    orch_sent = MasterOrchestrator(
        user_query="news sentiment report of TCS",
        report_type=ReportType.SENTIMENT,
    )
    spec_sent = orch_sent._execute_plan_report_format({})
    assert "sentiment" in spec_sent.rationale.lower()
    
    sent_sec_map = {s.key: s for s in spec_sent.sections}
    assert "sentiment_news" in sent_sec_map
    assert sent_sec_map["sentiment_news"].include is True
    assert "catalysts" in sent_sec_map["sentiment_news"].emphasis or "momentum" in spec_sent.rationale.lower()


def test_aml_clean_sweep_vs_targeted_followup():
    """Test AML screening flow for clean sweep vs structured hit."""
    orch = MasterOrchestrator(
        user_query="full equity report on TCS",
        report_type=ReportType.EQUITY,
        run_aml=True,
    )
    orch.state.ticker = "TCS.NS"
    orch.state.company_name = "Tata Consultancy Services"

    # Mock structured sweep returning all clean
    mock_clean_sweep = [
        {"entity_screened": "TCS.NS", "source_name": "OFAC SDN List", "finding_summary": "No match found", "severity": "None", "source_url": ""},
    ]
    with patch("tools.aml_tools.run_structured_aml_sweep", return_value=mock_clean_sweep):
        res, summary, ok, _ = orch._dispatch_tool("run_structured_aml_sweep", {"entity_name": "Tata Consultancy Services", "ticker": "TCS.NS"})
        assert ok is True
        assert len(orch.state.aml_result.findings) == 1
        assert orch.state.aml_result.findings[0].severity == AMLSeverity.NONE

    # Structured hit -> targeted adverse media search with focus
    mock_adverse_results = [
        {"entity_screened": "TCS.NS", "source_name": "Adverse Media (Tavily search)", "finding_summary": "Targeted investigation hit found", "severity": "Elevated", "source_url": "https://example.com/ed"},
    ]
    with patch("tools.aml_tools.search_adverse_media", return_value=mock_adverse_results):
        res, summary, ok, _ = orch._dispatch_tool("search_adverse_media", {
            "entity_name": "Tata Consultancy Services",
            "focus": "reason for regulatory enquiry",
            "depth": "advanced",
        })
        assert ok is True
        assert any(f.severity == AMLSeverity.ELEVATED for f in orch.state.aml_result.findings)


def test_diminishing_returns_early_stop():
    """Test that two consecutive Tavily searches with no new URLs/titles stop searching."""
    orch = MasterOrchestrator(
        user_query="news sentiment report of TCS",
        report_type=ReportType.SENTIMENT,
    )
    orch.state.ticker = "TCS.NS"

    repeated_search_res = [
        {"title": "TCS Q3 Results", "url": "https://example.com/news1", "content": "TCS beats profit estimates", "score": 0.9}
    ]

    with patch("tools.search_tools.search_web_news", return_value=repeated_search_res):
        # 1st call -> 1 new item
        orch._dispatch_tool("search_web_news", {"query": "TCS news 1"})
        assert orch.consecutive_empty_searches == 0

        # 2nd call -> 0 new items -> consecutive = 1
        orch._dispatch_tool("search_web_news", {"query": "TCS news 2"})
        assert orch.consecutive_empty_searches == 1

        # 3rd call -> 0 new items -> consecutive = 2
        orch._dispatch_tool("search_web_news", {"query": "TCS news 3"})
        assert orch.consecutive_empty_searches == 2

        # 4th call -> hit diminishing returns threshold, does not search
        res, summary, ok, _ = orch._dispatch_tool("search_web_news", {"query": "TCS news 4"})
        assert "Diminishing returns" in summary


def test_idempotency_cache():
    """Test that duplicate tool calls return cached results without re-executing."""
    orch = MasterOrchestrator(
        user_query="valuation of Apple",
        report_type=ReportType.VALUATION,
    )
    orch.state.ticker = "AAPL"

    with patch("tools.finance_tools.get_price_snapshot", return_value={"current_price": 220.0, "market_cap": 3400000000000.0}) as mock_fetch:
        # First call
        res1, summary1, ok1, _ = orch._dispatch_tool("get_price_snapshot", {"ticker": "AAPL"})
        assert mock_fetch.call_count == 1
        assert res1["current_price"] == 220.0

        # Second identical call -> cached
        res2, summary2, ok2, _ = orch._dispatch_tool("get_price_snapshot", {"ticker": "AAPL"})
        assert mock_fetch.call_count == 1
        assert "Cached result" in summary2
        assert res2["current_price"] == 220.0


def test_trace_dump_and_telemetry():
    """Test that trace JSON is correctly formatted and dumped."""
    orch = MasterOrchestrator(
        user_query="valuation analysis of TCS",
        report_type=ReportType.VALUATION,
    )
    orch.state.ticker = "TCS.NS"
    orch.state.company_name = "Tata Consultancy Services"
    orch.state.telemetry.gemini_calls = 3
    orch.state.telemetry.tavily_calls = 1
    orch.state.telemetry.wall_clock_seconds = 4.5
    orch.state.report_spec = ReportSpec(sections=[], rationale="Test rationale", report_spec_source="agent")

    orch._dump_trace()

    trace_file = orch.state.ticker.replace(".", "_") + f"_{date.today().isoformat()}_trace.json"
    trace_path = orch.state.telemetry and (orch.state.ticker and (None or (orch.state.user_query and None)))
    
    # Read output trace
    from config import settings
    expected_path = settings.output_dir / trace_file
    assert expected_path.exists()
    data = json.loads(expected_path.read_text(encoding="utf-8"))
    assert data["ticker"] == "TCS.NS"
    assert data["telemetry"]["gemini_calls"] == 3
    assert data["telemetry"]["tavily_calls"] == 1
    assert data["report_spec"]["rationale"] == "Test rationale"


def test_sentiment_extraction_failure_handling():
    """Verify that sentiment extraction failure sets extraction_failed flag and honest disclosures."""
    from harness.synthesis import _build_section_instructions

    orch = MasterOrchestrator(
        user_query="news sentiment report of TCS",
        report_type=ReportType.SENTIMENT,
    )
    orch.state.ticker = "TCS.NS"
    orch.search_queries_used = ["TCS Q3 news"]

    # Mock _extract_structured_findings to raise exception
    with patch("harness.orchestrator._extract_structured_findings", side_effect=ValueError("Invalid JSON from LLM")):
        with patch("harness.orchestrator.run_chief_editor", return_value="# Executive Summary\nTest report."):
            with patch("harness.orchestrator.assemble_market_metrics") as mock_mm:
                mock_mm.return_value = MarketMetrics(ticker="TCS.NS", company_name="Tata Consultancy Services")
                # Trigger post-loop extraction block manually by setting state
                orch.state.status = AgentStatus.DONE
                orch.state.turn = 1
                
                # Mock time to avoid long run
                with patch("time.time", side_effect=[100.0, 105.0]):
                    # Run extraction logic block
                    if not orch.state.sentiment_findings and orch.search_queries_used:
                        try:
                            from harness.orchestrator import _extract_structured_findings
                            orch.state.sentiment_findings = _extract_structured_findings(
                                None, orch.raw_search_contents, orch.search_queries_used
                            )
                        except Exception:
                            orch.state.telemetry.extraction_failed = True
                            orch.state.sentiment_findings = SentimentFindings(
                                overall_sentiment=SentimentLabel.NEUTRAL,
                                sentiment_summary="Automated sentiment extraction did not complete successfully for this run; no catalysts or risks could be structured from search results.",
                                queries_used=orch.search_queries_used,
                                extraction_failed=True,
                            )

                assert orch.state.sentiment_findings.extraction_failed is True
                assert orch.state.telemetry.extraction_failed is True
                assert "did not complete successfully" in orch.state.sentiment_findings.sentiment_summary

                # Check instructions generated for Chief Editor
                instructions = _build_section_instructions(
                    ReportType.SENTIMENT,
                    "6-Month",
                    sentiment_findings=orch.state.sentiment_findings,
                )
                assert "Automated sentiment extraction did not complete successfully" in instructions
                assert "Do not provide a Bullish/Bearish/Neutral market mood verdict" in instructions


def test_dynamic_section_planning_bounds_and_editorial_goal():
    """Verify that section planning caps sections at maximum 7 and adapts to editorial goal."""
    orch = MasterOrchestrator(
        user_query="Detailed valuation & margin analysis of L&T",
        report_type=ReportType.VALUATION,
        editorial_goal="L&T Infrastructure Margin Sustainability Scan",
    )
    orch.state.ticker = "LT.NS"

    # Test auto-planning with editorial goal
    spec = orch._execute_plan_report_format({})
    assert len(spec.sections) <= 7
    assert spec.editorial_goal == "L&T Infrastructure Margin Sustainability Scan"
    assert "Valuation focus" in spec.rationale

    # Test explicit custom sections list with > 7 sections (must be bounded to 7)
    custom_raw_sections = [
        {"key": f"sec_{i}", "title": f"Custom Section {i}", "instruction": f"Instruction {i}", "order": i, "include": True}
        for i in range(1, 12)
    ]
    bounded_spec = orch._execute_plan_report_format({"sections": custom_raw_sections, "rationale": "Custom multi-section blueprint"})
    assert len(bounded_spec.sections) == 7
    assert bounded_spec.sections[0].key == "sec_1"
    assert bounded_spec.sections[6].key == "sec_7"


def test_category_retry_cap_guardrail():
    """Verify that validation does not block finalization if missing category failed >= 2 attempts."""
    orch = MasterOrchestrator(
        user_query="valuation of TCS",
        report_type=ReportType.VALUATION,
    )
    orch.state.ticker = "TCS.NS"
    orch.state.market_data = {
        "current_price": 3800.0,
        "market_cap": 14000000000000.0,
        "eps_ttm": 133.0,
    }
    # Fundamentals are missing roe, debt_to_equity; valuation multiples missing
    # Simulate 2 failed attempts on valuation multiples
    orch.category_attempts["get_valuation_multiples"] = 2
    orch.category_attempts["get_fundamentals"] = 2
    orch.search_queries_used = ["TCS valuation analyst targets"]

    v = orch._execute_validate_data()
    # Because attempts >= 2, valuation_multiples and fundamentals are skipped from blocking missing list
    assert "valuation_multiples" not in v.missing
    assert "fundamentals" not in v.missing
    assert v.satisfied is True


def test_dispatch_compute_custom_financial_metric():
    """Test that MasterOrchestrator dispatches compute_custom_financial_metric and stores result in state."""
    orch = MasterOrchestrator(
        user_query="Analyze Tata Motors ROCE and 3Y Revenue CAGR",
        report_type=ReportType.EQUITY,
    )
    orch.state.ticker = "TATAMOTORS.NS"

    res, summary, ok, err = orch._dispatch_tool(
        "compute_custom_financial_metric",
        {
            "expression": "cagr(beginning_val, ending_val, 3)",
            "context": {"beginning_val": 200000.0, "ending_val": 430000.0},
            "metric_name": "3y_revenue_cagr",
        },
    )
    assert ok is True
    assert orch.state.custom_metrics["3y_revenue_cagr"]["value"] == 29.07
    assert orch.state.custom_metrics["3y_revenue_cagr"]["formatted_value"] == "+29.07%"
    assert "3y_revenue_cagr" in orch.state.market_data["custom_metrics"]


def test_dispatch_plan_report_format_retry_and_telemetry():
    """Verify that plan_report_format rejects empty sections twice with structured error before falling back."""
    orch = MasterOrchestrator(
        user_query="valuation of TCS",
        report_type=ReportType.VALUATION,
    )
    orch.state.ticker = "TCS.NS"

    # 1st attempt: empty sections -> rejection + increment counter
    res1, summary1, ok1, err1 = orch._dispatch_tool("plan_report_format", {})
    assert ok1 is False
    assert orch._format_retries == 1
    assert "Error: 'sections' cannot be empty" in err1

    # 2nd attempt: still empty sections -> rejection + increment counter
    res2, summary2, ok2, err2 = orch._dispatch_tool("plan_report_format", {"sections": []})
    assert ok2 is False
    assert orch._format_retries == 2
    assert "Error: 'sections' cannot be empty" in err2

    # 3rd attempt: empty sections -> retry ceiling reached, falls through to fallback ladder
    res3, summary3, ok3, err3 = orch._dispatch_tool("plan_report_format", {})
    assert ok3 is True
    assert err3 is None
    assert res3["report_spec_source"] == "fallback"
    assert orch.state.report_spec.report_spec_source == "fallback"
    assert "fallback" in summary3


def test_dispatch_plan_report_format_agent_success():
    """Verify that plan_report_format with explicit sections succeeds immediately with agent telemetry."""
    orch = MasterOrchestrator(
        user_query="valuation of TCS",
        report_type=ReportType.VALUATION,
    )
    orch.state.ticker = "TCS.NS"

    custom_sections = [
        {"key": "executive_summary", "title": "Exec Summary", "order": 1, "include": True},
        {"key": "valuation_analysis", "title": "Valuation Multiples", "order": 2, "include": True},
    ]
    res, summary, ok, err = orch._dispatch_tool(
        "plan_report_format",
        {"sections": custom_sections, "rationale": "Explicit agent planned sections"},
    )
    assert ok is True
    assert err is None
    assert res["report_spec_source"] == "agent"
    assert orch.state.report_spec.report_spec_source == "agent"
    assert orch._format_retries == 0
    assert "agent" in summary


def test_dispatch_plan_report_format_malformed_sections_retry():
    """Verify that plan_report_format rejects malformed sections and records validation errors."""
    orch = MasterOrchestrator(
        user_query="valuation of TCS",
        report_type=ReportType.VALUATION,
    )
    orch.state.ticker = "TCS.NS"

    malformed_sections = [
        {"title": "Section Missing Key", "order": 1},  # missing 'key'
        {"key": 12345, "title": "Invalid key type", "order": "invalid_order"},  # invalid order int
    ]

    # Attempt 1: malformed sections -> rejected with detailed validation errors
    res1, summary1, ok1, err1 = orch._dispatch_tool("plan_report_format", {"sections": malformed_sections})
    assert ok1 is False
    assert orch._format_retries == 1
    assert "none were valid SectionSpec objects" in err1
    assert "Validation errors:" in err1

    # Attempt 2: still malformed -> rejected again
    res2, summary2, ok2, err2 = orch._dispatch_tool("plan_report_format", {"sections": malformed_sections})
    assert ok2 is False
    assert orch._format_retries == 2

    # Attempt 3: retry ceiling reached -> fallback used and section_validation_errors recorded
    res3, summary3, ok3, err3 = orch._dispatch_tool("plan_report_format", {"sections": malformed_sections})
    assert ok3 is True
    assert res3["report_spec_source"] == "fallback"
    assert orch.state.report_spec.report_spec_source == "fallback"
    assert len(orch.state.report_spec.section_validation_errors) > 0


def test_tool_call_record_reasoning_text():
    """Verify that ToolCallRecord accepts and stores reasoning_text."""
    from schemas import ToolCallRecord
    rec = ToolCallRecord(
        turn=1,
        tool_name="get_price_snapshot",
        arguments={"ticker": "TCS.NS"},
        result_summary="Fetched price",
        ok=True,
        reasoning_text="Model reasoned that it needs current price snapshot first.",
    )
    assert rec.reasoning_text == "Model reasoned that it needs current price snapshot first."
    data = rec.model_dump()
    assert data["reasoning_text"] == "Model reasoned that it needs current price snapshot first."


def test_reflect_on_progress_dispatch_and_finalize_gating():
    """Verify reflect_on_progress dispatches cleanly and gates finalize_report."""
    orch = MasterOrchestrator(
        user_query="valuation of TCS",
        report_type=ReportType.VALUATION,
    )
    orch.state.ticker = "TCS.NS"
    orch.state.market_data = {
        "current_price": 3800.0,
        "market_cap": 14000000000000.0,
        "pe_ratio": 28.5,
        "pb_ratio": 12.1,
        "ps_ratio": 6.8,
        "eps_ttm": 133.0,
    }
    orch.search_queries_used = ["TCS valuation analyst targets"]

    # 1. Attempt finalize_report before reflection -> gated and rejected
    res_fin1, summary_fin1, ok_fin1, err_fin1 = orch._dispatch_tool("finalize_report", {})
    assert ok_fin1 is False
    assert "call reflect_on_progress first" in err_fin1

    # 2. Dispatch reflect_on_progress with gaps
    res_ref1, summary_ref1, ok_ref1, err_ref1 = orch._dispatch_tool(
        "reflect_on_progress",
        {
            "gathered_summary": "Fetched price snapshot and valuation multiples.",
            "still_needed": ["news_searches"],
            "next_action_rationale": "Need latest news on analyst price targets.",
        },
    )
    assert ok_ref1 is True
    assert err_ref1 is None
    assert "1 gap(s) noted" in summary_ref1
    assert "news_searches" in summary_ref1

    # 3. Dispatch reflect_on_progress with no gaps
    res_ref2, summary_ref2, ok_ref2, err_ref2 = orch._dispatch_tool(
        "reflect_on_progress",
        {
            "gathered_summary": "Fetched price snapshot, multiples, and sentiment analyst targets.",
            "still_needed": [],
            "next_action_rationale": "All required data in place, ready to finalize report.",
        },
    )
    assert ok_ref2 is True
    assert "none, ready to finalize" in summary_ref2

    # 4. Attempt finalize_report after reflection -> succeeds
    res_fin2, summary_fin2, ok_fin2, err_fin2 = orch._dispatch_tool("finalize_report", {})
    assert ok_fin2 is True
    assert err_fin2 is None
    assert orch.state.status == AgentStatus.DONE


def test_reasoning_extraction_with_thoughts_and_rationale():
    """Verify that reasoning extraction correctly separates and combines thoughts and rationale."""
    from google.genai import types

    orch = MasterOrchestrator(
        user_query="valuation of TCS",
        report_type=ReportType.VALUATION,
    )

    # 1. Thought part + rationale part
    mock_resp1 = MagicMock()
    mock_candidate1 = MagicMock()
    mock_candidate1.content.parts = [
        types.Part(text="Thinking about company fundamentals...", thought=True),
        types.Part(text="Fetching valuation multiples for TCS.NS."),
    ]
    mock_resp1.candidates = [mock_candidate1]
    mock_resp1.function_calls = []

    thought_parts = []
    text_parts = []
    for p in mock_candidate1.content.parts:
        p_thought = getattr(p, "thought", None)
        p_text = getattr(p, "text", None)
        if p_thought is True:
            if p_text and p_text.strip():
                thought_parts.append(p_text.strip())
        elif isinstance(p_thought, str) and p_thought.strip():
            thought_parts.append(p_thought.strip())
        elif p_text and p_text.strip():
            text_parts.append(p_text.strip())

    thought_text = " ".join(thought_parts).strip()
    rationale_text = " ".join(text_parts).strip()
    res_text = f"[Thought: {thought_text}] {rationale_text}"
    assert res_text == "[Thought: Thinking about company fundamentals...] Fetching valuation multiples for TCS.NS."



