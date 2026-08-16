from schemas import AMLFinding, AMLSeverity
from harness.synthesis import _finding_sort_key
from tools.aml_tools import (
    _name_matches,
    _normalize,
    get_fatf_risk,
    get_jurisdictional_risk,
)
from harness.aml_agent_loop import _classify_severity


def test_normalize():
    assert _normalize("  Reliance   Industries   LTD ") == "reliance industries ltd"


def test_name_matches():
    # Exact / Substring
    assert _name_matches("Reliance Industries", "Reliance Industries Limited") is True
    assert _name_matches("TATA CONSULTANCY SERVICES", "Tata Consultancy Services Ltd") is True
    
    # Significant word overlap
    assert _name_matches("Mukesh Ambani", "Ambani, Mukesh Dhirubhai") is True
    
    # Negative cases
    assert _name_matches("Apple Inc", "Microsoft Corporation") is False
    assert _name_matches("State Bank of India", "HDFC Bank Limited") is False


def test_classify_severity():
    # High keywords
    assert _classify_severity("Entity was sanctioned by OFAC for money laundering.") == AMLSeverity.HIGH
    assert _classify_severity("Found guilty of terror financing and debarred.") == AMLSeverity.HIGH
    
    # Elevated keywords
    assert _classify_severity("SEBI order passed against the promoters for insider trading.") == AMLSeverity.ELEVATED
    assert _classify_severity("Bribery and corruption investigation opened under FCPA.") == AMLSeverity.ELEVATED
    
    # Watch fallback
    assert _classify_severity("Company attended regular business summit.") == AMLSeverity.WATCH


def test_jurisdictional_risk():
    # Elevated risk jurisdiction (e.g. India CPI 39)
    in_risk = get_jurisdictional_risk("IN")
    assert in_risk.severity == AMLSeverity.ELEVATED
    assert "India" in in_risk.finding_summary
    assert "Transparency International CPI 2023" in in_risk.source_name

    # Low risk jurisdiction (e.g. Singapore CPI 83)
    sg_risk = get_jurisdictional_risk("SG")
    assert sg_risk.severity == AMLSeverity.NONE

    # Unknown jurisdiction
    unknown = get_jurisdictional_risk("ZZ")
    assert unknown.severity == AMLSeverity.WATCH


def test_fatf_risk():
    # Black list
    iran = get_fatf_risk("Iran")
    assert iran.severity == AMLSeverity.HIGH
    assert "Black List" in iran.finding_summary

    # Grey list
    sa = get_fatf_risk("South Africa")
    assert sa.severity == AMLSeverity.ELEVATED
    assert "Grey List" in sa.finding_summary

    # Not listed
    india = get_fatf_risk("India")
    assert india.severity == AMLSeverity.NONE


def test_finding_sort_order():
    high = AMLFinding(
        entity_screened="Test",
        source_name="OFAC",
        finding_summary="Name match found in OFAC list",
        severity=AMLSeverity.HIGH,
        source_url="https://example.com",
    )
    elevated = AMLFinding(
        entity_screened="Test",
        source_name="SEBI",
        finding_summary="Potential match under SEBI order",
        severity=AMLSeverity.ELEVATED,
        source_url="https://example.com",
    )
    clean = AMLFinding(
        entity_screened="Test",
        source_name="UN",
        finding_summary="No match found",
        severity=AMLSeverity.NONE,
        source_url="https://example.com",
    )
    failed = AMLFinding(
        entity_screened="Test",
        source_name="World Bank",
        finding_summary="Could not fetch World Bank debarment list. Manual check recommended.",
        severity=AMLSeverity.WATCH,
        source_url="https://example.com",
    )

    findings = [clean, failed, high, elevated]
    sorted_findings = sorted(findings, key=_finding_sort_key)

    # Expected order: High (10) -> Elevated (20) -> Clean (40) -> Failed (50)
    assert sorted_findings[0].severity == AMLSeverity.HIGH
    assert sorted_findings[1].severity == AMLSeverity.ELEVATED
    assert sorted_findings[2].severity == AMLSeverity.NONE
    assert "Could not fetch" in sorted_findings[3].finding_summary
