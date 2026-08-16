"""
CLI entry point for the Agentic Financial Report Generator.

Target Architecture:
  1. Intake (Seed Priors)    - Single-shot LLM calls for company and report type priors
  2. Master Orchestrator Loop- AGENTIC: ReAct loop (Reason -> Act -> Observe -> Validate -> Plan -> Finalize)
                               Granular deterministic tools, human disambiguation, dynamic ReportSpec
  3. PDF Generator           - Deterministic: FinalReport -> PDF file
"""
from __future__ import annotations

import argparse
import logging
import sys

from harness.intake import detect_report_type, extract_company_reference, extract_editorial_goal, extract_intake_priors
from harness.orchestrator import run_orchestrator
from tools.pdf_tools import compile_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("main")


def generate_report(user_query: str, run_aml: bool = False) -> None:
    print(f"\n> Request: {user_query}\n")

    # Natural language AML check
    query_lower = user_query.lower()
    if not run_aml and any(term in query_lower for term in ("aml", "sanctions", "compliance", "abc risk", "debarment")):
        run_aml = True
        print("      Note: Natural language AML screening intent detected -> enabling Layer 2 compliance screening.")

    print("[1/3] Identifying initial company reference, report type, and editorial goal...")
    try:
        priors = extract_intake_priors(user_query)
        company_reference = priors["company_reference"]
        report_type = priors["report_type"]
        editorial_goal = priors["editorial_goal"]
    except Exception as exc:
        logger.warning("Intake extraction fallback: %s", exc)
        company_reference = None
        report_type = detect_report_type(user_query)
        editorial_goal = extract_editorial_goal(user_query)

    print(f"      -> Prior entity: {company_reference or 'Unspecified'}  |  Report type: {report_type.value}")
    if editorial_goal:
        print(f"      -> Editorial goal: {editorial_goal}")

    print("[2/3] Executing master agentic orchestrator loop...")
    agent_state, report = run_orchestrator(
        user_query=user_query,
        initial_company_ref=company_reference,
        report_type=report_type,
        editorial_goal=editorial_goal,
        run_aml=run_aml,
    )

    print(f"      -> Resolved ticker: {report.ticker} ({report.company_name})")
    print(f"      -> Completed in {agent_state.turn} turn(s) with {len(agent_state.tool_log)} tool call(s)")
    print(f"      -> Telemetry: {agent_state.telemetry.gemini_calls} Gemini calls, "
          f"{agent_state.telemetry.tavily_calls}/{agent_state.telemetry.tavily_calls_budget} Tavily calls, "
          f"{agent_state.telemetry.wall_clock_seconds}s wall clock")

    if report.report_spec:
        print(f"      -> ReportSpec: {len(report.report_spec.sections)} sections configured. "
              f"Rationale: {report.report_spec.rationale}")

    print("[3/3] Rendering PDF...")
    pdf_path = compile_pdf(report)
    print(f"\nDone. Report saved to: {pdf_path}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agentic Financial Report Generator")
    parser.add_argument(
        "query",
        type=str,
        help=(
            'Natural language report request. Examples:\n'
            '  "news sentiment report of Reliance Industries"\n'
            '  "valuation analysis of Apple"\n'
            '  "full equity report on TCS"\n'
            '  "valuation report of Tata"\n'
        ),
    )
    parser.add_argument(
        "--aml",
        action="store_true",
        default=False,
        help=(
            "Run the AML/ABC compliance screening layer (Layer 2). "
            "Screens the company against OFAC SDN, OpenSanctions, World Bank debarment, "
            "UN Consolidated List, EU Sanctions, SEC EDGAR FCPA releases, "
            "TI CPI jurisdictional risk, FATF grey/black list, and Tavily adverse media. "
            "All sources are free/public."
        ),
    )
    args = parser.parse_args()

    try:
        generate_report(args.query, run_aml=args.aml)
    except Exception:
        logger.exception("Report generation failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
