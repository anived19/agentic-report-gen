"""
Bounded adverse-media search loop for the AML/ABC screening pipeline.

Mirrors the pattern in harness/agent_loop.py but with:
- A different system prompt (agents/aml_screener.md)
- A tighter turn budget (2 turns — one broad sweep, one targeted follow-up)
- AML-focused output schema (list of AMLFinding, not SentimentFindings)
- No Phase B structured extraction — AML findings are assembled directly
  from tool call results, not synthesized by the LLM, to prevent
  paraphrasing of regulatory findings.

The LLM's only job here is to formulate effective search queries. The
actual findings are assembled from the raw search results by this code,
not by the model.
"""
from __future__ import annotations

import logging
import re

from google import genai
from google.genai import types

from config import settings
from harness.gemini_retry import generate_with_retry
from harness.md_loader import load_agent_prompt, load_skill
from schemas import AMLFinding, AMLSeverity

logger = logging.getLogger(__name__)

_AML_MAX_TURNS = 3   # tight budget: 1 broad sweep + 1-2 targeted follow-ups

# Keywords that elevate a search result's severity
_HIGH_SEVERITY_KEYWORDS = [
    "sanctioned", "sanctions", "debarred", "debarment", "convicted",
    "indicted", "arrested", "money laundering", "aml", "terror financing",
    "wilful default",
]
_ELEVATED_KEYWORDS = [
    "sebi order", "sebi adjudication", "enforcement directorate", "ed raid",
    "bribery", "corruption", "fcpa", "sfo investigation", "nca", "interpol",
    "fraud", "ponzi", "insider trading", "price manipulation",
]


def _classify_severity(text: str) -> AMLSeverity:
    t = text.lower()
    if any(kw in t for kw in _HIGH_SEVERITY_KEYWORDS):
        return AMLSeverity.HIGH
    if any(kw in t for kw in _ELEVATED_KEYWORDS):
        return AMLSeverity.ELEVATED
    return AMLSeverity.WATCH


def run_aml_adverse_media_agent(
    company_name: str,
    ticker: str,
) -> list[AMLFinding]:
    """
    Run a bounded Tavily search loop with AML-focused queries.
    Returns AMLFinding objects built from raw search results.
    """
    client = genai.Client(api_key=settings.gemini_api_key)
    system_prompt = load_agent_prompt("aml_screener")
    skill = load_skill("screen_entity_aml")

    tool = types.Tool(function_declarations=[skill.declaration])
    loop_config = types.GenerateContentConfig(system_instruction=system_prompt, tools=[tool])

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[types.Part(text=(
                f"Conduct an AML/ABC adverse-media and regulatory screening sweep for: "
                f"{company_name} (ticker: {ticker}).\n\n"
                f"Run searches targeting:\n"
                f"1. SEBI enforcement orders or adjudication against {company_name}\n"
                f"2. Enforcement Directorate (ED) actions against {company_name}\n"
                f"3. Bribery, corruption, or FCPA violations involving {company_name}\n"
                f"4. UK SFO or NCA investigations involving {company_name}\n"
                f"Use your search tool to find regulatory press releases and adverse media. "
                f"Do not fabricate any finding — only report what you actually retrieve."
            ))]
        )
    ]

    raw_results: list[dict] = []

    for _ in range(_AML_MAX_TURNS):
        response = generate_with_retry(
            client,
            model=settings.gemini_model,
            contents=contents,
            config=loop_config,
        )
        if response.candidates and response.candidates[0].content:
            contents.append(response.candidates[0].content)

        calls = response.function_calls or []
        if not calls:
            break

        function_response_parts = []
        for call in calls:
            args = call.args or {}
            if call.name == skill.name:
                try:
                    result = skill.function(**args)
                    raw_results.extend(result)
                    payload = {"result": result}
                except Exception as exc:
                    payload = {"error": str(exc)}
            else:
                payload = {"error": f"Unknown tool: {call.name}"}
            function_response_parts.append(
                types.Part.from_function_response(name=call.name, response=payload)
            )
        contents.append(types.Content(role="user", parts=function_response_parts))

    # Build AMLFinding objects directly from raw results — no LLM synthesis
    findings: list[AMLFinding] = []
    seen_urls: set[str] = set()

    for item in raw_results:
        url = item.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        content = item.get("content", "") or item.get("title", "")
        severity = _classify_severity(content)
        if severity in (AMLSeverity.WATCH, AMLSeverity.ELEVATED, AMLSeverity.HIGH):
            findings.append(AMLFinding(
                entity_screened=company_name,
                source_name="Adverse Media (Tavily search)",
                finding_summary=(content[:300] + "…") if len(content) > 300 else content,
                severity=severity,
                source_url=url,
            ))

    if not findings:
        findings.append(AMLFinding(
            entity_screened=company_name,
            source_name="Adverse Media (Tavily search)",
            finding_summary=(
                "No adverse regulatory, enforcement, or AML/ABC-related media was found "
                "for this entity in this search cycle. This does not constitute clearance."
            ),
            severity=AMLSeverity.NONE,
            source_url="",
        ))

    return findings
