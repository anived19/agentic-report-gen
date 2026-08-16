from pathlib import Path
import pytest

from schemas import (
    FinalReport,
    MarketMetrics,
    PricePoint,
    ReportType,
    SentimentFindings,
    SentimentLabel,
)
from tools.pdf_tools import (
    _render_full_html,
    _render_markdown_to_html,
    compile_pdf,
)


def test_render_markdown_to_html():
    md_text = "# Executive Summary\n\nThis is a **bold** test."
    html = _render_markdown_to_html(md_text)
    assert "<h1>Executive Summary</h1>" in html
    assert "<strong>bold</strong>" in html


def test_render_full_html():
    metrics = MarketMetrics(
        ticker="AAPL",
        company_name="Apple Inc.",
        current_price=150.0,
        fifty_day_ma=145.0,
        two_hundred_day_ma=140.0,
        outlook_price_trend=[
            PricePoint(date="2026-01-01", close=140.0),
            PricePoint(date="2026-02-01", close=150.0),
        ],
    )
    sentiment = SentimentFindings(
        overall_sentiment=SentimentLabel.BULLISH,
        sentiment_summary="Positive market outlook.",
        key_catalysts=[],
        key_risks=[],
    )
    report = FinalReport(
        ticker="AAPL",
        company_name="Apple Inc.",
        report_type=ReportType.EQUITY,
        markdown_body="# Executive Summary\nStrong hardware sales.",
        market_metrics=metrics,
        sentiment_findings=sentiment,
    )
    html = _render_full_html(report)
    assert "Apple Inc." in html
    assert "AAPL" in html
    assert "Executive Summary" in html


def test_compile_pdf_smoke(tmp_path: Path):
    metrics = MarketMetrics(
        ticker="TEST",
        company_name="Test Corp",
        current_price=100.0,
    )
    sentiment = SentimentFindings(
        overall_sentiment=SentimentLabel.NEUTRAL,
        sentiment_summary="Neutral test.",
        key_catalysts=[],
        key_risks=[],
    )
    report = FinalReport(
        ticker="TEST",
        company_name="Test Corp",
        report_type=ReportType.GENERAL,
        markdown_body="# Overview\nTest PDF generation.",
        market_metrics=metrics,
        sentiment_findings=sentiment,
    )

    out_file = tmp_path / "test_report.pdf"
    pdf_path = compile_pdf(report, output_path=out_file)

    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0
