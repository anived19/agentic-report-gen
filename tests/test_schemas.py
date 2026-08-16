from datetime import date
import pytest
from pydantic import ValidationError

from schemas import (
    AMLFinding,
    AMLScreeningResult,
    AMLSeverity,
    CitedClaim,
    FinalReport,
    MarketMetrics,
    ReportType,
    SentimentFindings,
    SentimentLabel,
    format_currency_amount,
)


def test_format_currency_amount():
    # INR formatting
    assert format_currency_amount(1.7727538331648e13, "INR") == "₹17.73 Lakh Cr"
    assert format_currency_amount(5000000000.0, "INR") == "₹500.00 Cr"
    assert format_currency_amount(2500000.0, "INR") == "₹25.00 Lakhs"
    assert format_currency_amount(15000.5, "INR") == "₹15,000.50"

    # USD / other formatting
    assert format_currency_amount(3120000000000.0, "USD") == "$3.12T"
    assert format_currency_amount(500200000000.0, "USD") == "$500.20B"
    assert format_currency_amount(45500000.0, "USD") == "$45.50M"
    assert format_currency_amount(750000.0, "USD") == "$750.00K"
    assert format_currency_amount(123.45, "USD") == "$123.45"

    # None handling
    assert format_currency_amount(None) is None


def test_cited_claim_valid():
    claim = CitedClaim(
        claim="Revenue increased 15% YoY.",
        source_url="https://finance.yahoo.com/news/123.html",
    )
    assert claim.claim == "Revenue increased 15% YoY."
    assert str(claim.source_url) == "https://finance.yahoo.com/news/123.html"


def test_cited_claim_invalid_url():
    with pytest.raises(ValidationError):
        CitedClaim(claim="Some claim", source_url="not-a-valid-url")


def test_cited_claim_empty_claim():
    with pytest.raises(ValidationError):
        CitedClaim(claim="   ", source_url="https://example.com")


def test_sentiment_findings():
    findings = SentimentFindings(
        overall_sentiment=SentimentLabel.BULLISH,
        sentiment_summary="Strong earnings outlook.",
        key_catalysts=[
            CitedClaim(claim="Cloud growth accelerating", source_url="https://example.com/cloud")
        ],
        key_risks=[
            CitedClaim(claim="Margin pressure in retail", source_url="https://example.com/margin")
        ],
        queries_used=["TCS cloud growth Q2"],
    )
    assert findings.overall_sentiment == SentimentLabel.BULLISH
    assert len(findings.key_catalysts) == 1
    assert len(findings.key_risks) == 1


def test_aml_finding_and_result():
    finding = AMLFinding(
        entity_screened="Test Corp",
        source_name="OFAC SDN List",
        finding_summary="No match found in OFAC SDN list.",
        severity=AMLSeverity.NONE,
        source_url="https://sanctionslist.ofac.treas.gov",
    )
    result = AMLScreeningResult(
        entities_screened=["Test Corp"],
        findings=[finding],
        screened_at=date.today(),
    )
    assert len(result.findings) == 1
    assert result.findings[0].severity == AMLSeverity.NONE


def test_final_report_assembly():
    metrics = MarketMetrics(ticker="TCS.NS", company_name="Tata Consultancy Services")
    sentiment = SentimentFindings(
        overall_sentiment=SentimentLabel.NEUTRAL,
        sentiment_summary="Neutral outlook.",
        key_catalysts=[],
        key_risks=[],
    )
    report = FinalReport(
        ticker="TCS.NS",
        company_name="Tata Consultancy Services",
        report_type=ReportType.EQUITY,
        markdown_body="# Executive Summary\nSolid performance.",
        market_metrics=metrics,
        sentiment_findings=sentiment,
    )
    assert report.ticker == "TCS.NS"
    assert report.report_type == ReportType.EQUITY
    assert report.aml_result is None
