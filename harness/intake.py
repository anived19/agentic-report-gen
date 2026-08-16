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


def detect_report_type(user_query: str) -> ReportType:
    """
    Classify `user_query` into one of the four ReportType values.

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
