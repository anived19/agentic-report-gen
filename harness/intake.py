"""
Query intake: pulls a company/ticker reference and a report type out of
the user's free-text request.

Two single-shot LLM calls, no tools — these are classification tasks, not
decisions that need autonomy, so they're built the same way as the Chief
Editor: one generate_content call each, no loop, no agentic behavior.
"""
from __future__ import annotations

import logging

from google import genai
from google.genai import types

from config import settings
from harness.gemini_retry import generate_with_retry
from schemas import ReportType

logger = logging.getLogger(__name__)

_COMPANY_SYSTEM_PROMPT = (
    "Extract the company, group, or stock reference the user is asking about from their "
    "request. Respond with ONLY the exact company or group name as plain text — "
    "no punctuation, no explanation, no surrounding quotes.\n\n"
    "CRITICAL RULE: If the user refers to a conglomerate or business group (e.g. 'Tata', "
    "'Adani', 'Reliance', 'Mahindra', 'Bajaj', 'Aditya Birla', 'HDFC', 'ICICI'), "
    "output ONLY that exact group name (e.g. 'Tata', NOT 'Tata Consultancy Services' "
    "or 'Tata Motors'). Do not invent, extrapolate, or guess a specific subsidiary — "
    "disambiguation will be handled by the downstream system."
)

_REPORT_TYPE_SYSTEM_PROMPT = (
    "Classify the user's financial report request into exactly one of these "
    "four categories. Respond with ONLY the single lowercase word — nothing else.\n\n"
    "Categories:\n"
    "  sentiment  — the user wants news sentiment, recent headlines, market mood, "
    "               or a short-term outlook based on news (e.g. 'sentiment report', "
    "               'what is the news saying', 'market mood').\n"
    "  valuation  — the user wants valuation multiples, fair value, analyst price "
    "               targets, or intrinsic value analysis (e.g. 'is the stock cheap', "
    "               'P/E analysis', 'valuation report', 'overvalued?').\n"
    "  equity     — the user wants a comprehensive equity analysis covering "
    "               technicals, valuation AND sentiment together (e.g. 'full equity "
    "               report', 'deep dive', 'investment thesis').\n"
    "  general    — anything that doesn't clearly fit the above (e.g. generic "
    "               'stock report' with no specific angle).\n\n"
    "Do not explain your answer. Output only one of: sentiment, valuation, equity, general."
)


_EDITORIAL_GOAL_SYSTEM_PROMPT = (
    "Extract a concise, professional financial research editorial goal or theme summarizing "
    "the specific analytical intent of the user's request.\n"
    "Examples:\n"
    "  - 'Post-Demerger Valuation & Margin Sustainability Scan'\n"
    "  - 'Comprehensive Equity Research & Technical Momentum'\n"
    "  - 'News Sentiment, Catalysts & Downside Risk Brief'\n"
    "  - 'Deep Fundamental Valuation & Fair Value Convergence Model'\n"
    "  - 'Capital Structure & Ownership Concentration Analysis'\n\n"
    "Respond with ONLY the short title string (under 10 words) — no explanation, no quotes."
)


def extract_company_reference(user_query: str) -> str:
    """Returns a plain-text company/ticker reference extracted from `user_query`."""
    client = genai.Client(api_key=settings.gemini_api_key)
    response = generate_with_retry(
        client,
        model=settings.gemini_model,
        contents=[types.Content(role="user", parts=[types.Part(text=user_query)])],
        config=types.GenerateContentConfig(system_instruction=_COMPANY_SYSTEM_PROMPT),
    )
    reference = (response.text or "").strip().strip('"').strip("'")
    if not reference:
        raise ValueError("Could not extract a company reference from the query")

    logger.info("Intake extracted company reference: %r", reference)
    return reference


def extract_editorial_goal(user_query: str) -> str:
    """
    Extract a dynamic analytical framing / editorial goal for the report.
    E.g. 'Post-Demerger Valuation & Margin Sustainability Scan'.
    """
    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        response = generate_with_retry(
            client,
            model=settings.gemini_model,
            contents=[types.Content(role="user", parts=[types.Part(text=user_query)])],
            config=types.GenerateContentConfig(system_instruction=_EDITORIAL_GOAL_SYSTEM_PROMPT),
        )
        goal = (response.text or "").strip().strip('"').strip("'")
        if goal:
            logger.info("Intake extracted editorial goal: %r", goal)
            return goal
    except Exception as exc:
        logger.warning("Editorial goal extraction failed: %s — falling back to query-based framing", exc)

    # Fallback to a clean sanitized version of the query
    return f"Financial Intelligence Assessment: {user_query.strip()}"


def detect_report_type(user_query: str) -> ReportType:
    """
    Classify `user_query` into one of the ReportType values.

    Falls back to ReportType.GENERAL on any parse failure — a misclassification
    degrades report focus but never crashes the pipeline.
    """
    client = genai.Client(api_key=settings.gemini_api_key)
    response = generate_with_retry(
        client,
        model=settings.gemini_model,
        contents=[types.Content(role="user", parts=[types.Part(text=user_query)])],
        config=types.GenerateContentConfig(system_instruction=_REPORT_TYPE_SYSTEM_PROMPT),
    )
    raw = (response.text or "").strip().lower()
    try:
        report_type = ReportType(raw)
        logger.info("Intake classified report type: %r -> %s", raw, report_type)
        return report_type
    except ValueError:
        logger.warning("Could not parse report type from %r — defaulting to GENERAL", raw)
        return ReportType.GENERAL


def extract_intake_priors(user_query: str) -> tuple[Optional[str], ReportType, str]:
    """
    Convenience helper that extracts company reference, report type, and dynamic editorial goal.
    """
    try:
        company_ref = extract_company_reference(user_query)
    except Exception as exc:
        logger.warning("Could not extract company reference: %s", exc)
        company_ref = None

    report_type = detect_report_type(user_query)
    editorial_goal = extract_editorial_goal(user_query)

    return company_ref, report_type, editorial_goal

