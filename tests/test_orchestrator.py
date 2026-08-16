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
    orch.state.report_spec = ReportSpec(sections=[], rationale="Test rationale")

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
