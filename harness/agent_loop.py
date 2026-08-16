"""
Bounded ReAct loop for the Research/Sentiment agent.

This is the one place in the pipeline where the LLM genuinely decides what
to do next — which search queries to issue, whether to search again, when
it has enough to conclude. Market Data and Chief Editor are deliberately
NOT built this way (see ARCHITECTURE.md): this loop exists specifically
because sentiment synthesis needs judgment a fixed code path can't supply.

Two-phase design:
  Phase A - tool-calling loop, hard-capped at
            config.research_agent_max_turns: the model can call
            `search_web_news` repeatedly until it stops on its own or the
            turn budget runs out.
  Phase B - one final call, same conversation, tools disabled, with a
            JSON schema constraint, to get a clean SentimentFindings
            object. Kept separate from Phase A rather than hoping the
            model spontaneously emits valid JSON while also juggling tool
            calls. Retries once with the validation error fed back if the
            first attempt doesn't parse.

Function-response wiring (payload shape `{"result": ...}` / `{"error": ...}`,
role="user" for tool results) mirrors google-genai's own internal automatic
function-calling implementation, verified against the installed SDK
version rather than assumed.
"""
from __future__ import annotations

import logging

from google import genai
from google.genai import types
from pydantic import ValidationError

from config import settings
from harness.gemini_retry import generate_with_retry
from harness.md_loader import load_agent_prompt, load_skill
from schemas import ReportType, SentimentFindings

logger = logging.getLogger(__name__)


def _sentiment_response_json_schema() -> dict:
    """
    Hand-flattened JSON schema (no $ref/$defs) for the Phase B extraction
    call. SentimentFindings.model_json_schema() produces a $ref-based
    schema whose support under Gemini's structured output isn't something
    verifiable without live API access — flattening this small schema by
    hand sidesteps that risk entirely. Pydantic validation on the parsed
    result remains the real correctness gate either way.
    """
    cited_claim = {
        "type": "object",
        "properties": {
            "claim": {"type": "string"},
            "source_url": {"type": "string"},
        },
        "required": ["claim", "source_url"],
    }
    return {
        "type": "object",
        "properties": {
            "overall_sentiment": {"type": "string", "enum": ["Bullish", "Bearish", "Neutral"]},
            "sentiment_summary": {"type": "string"},
            "key_catalysts": {"type": "array", "items": cited_claim},
            "key_risks": {"type": "array", "items": cited_claim},
        },
        "required": ["overall_sentiment", "sentiment_summary", "key_catalysts", "key_risks"],
    }


def run_research_agent(
    company_name: str,
    ticker: str,
    report_type: ReportType = ReportType.GENERAL,
) -> SentimentFindings:
    """Run the bounded Research/Sentiment agent loop and return validated findings."""
    client = genai.Client(api_key=settings.gemini_api_key)
    system_prompt = load_agent_prompt("research_analyst")
    skill = load_skill("search_web_news")

    tool = types.Tool(function_declarations=[skill.declaration])
    loop_config = types.GenerateContentConfig(system_instruction=system_prompt, tools=[tool])

    # Tailor the initial research brief to the report type so the agent
    # knows which angles to prioritise without needing to change the tool.
    _type_guidance: dict[ReportType, str] = {
        ReportType.SENTIMENT: (
            "Focus on: recent news headlines, analyst rating changes, management "
            "commentary, and sector/macro events that drive short-term sentiment. "
            "Avoid searching for valuation multiples — those are already provided."
        ),
        ReportType.VALUATION: (
            "Focus on: analyst price targets, fair value estimates, earnings "
            "surprises vs. consensus, and any commentary on whether the stock "
            "looks cheap or expensive relative to peers. Sentiment is secondary."
        ),
        ReportType.EQUITY: (
            "This is a comprehensive equity report. Cover all angles: recent news "
            "sentiment, analyst targets and ratings, earnings quality, and any "
            "sector or macro tailwinds/headwinds. Use your full turn budget."
        ),
        ReportType.GENERAL: (
            "Provide a balanced overview: recent news sentiment, any notable "
            "analyst commentary, and the key near-term catalysts and risks."
        ),
    }
    guidance = _type_guidance.get(report_type, _type_guidance[ReportType.GENERAL])

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        f"Research current market information for {company_name} "
                        f"(ticker: {ticker}). Report type requested: {report_type.value}.\n\n"
                        f"Research brief: {guidance}\n\n"
                        f"Use your search tool to find recent, cited information, then "
                        f"produce an overall sentiment call with cited catalysts and risks."
                    )
                )
            ],
        )
    ]

    queries_used: list[str] = []
    concluded_naturally = False

    for _ in range(settings.research_agent_max_turns):
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
            concluded_naturally = True
            break

        function_response_parts = []
        for call in calls:
            args = call.args or {}
            if call.name != skill.name:
                logger.warning("Model requested unknown tool: %s", call.name)
                payload = {"error": f"Unknown tool: {call.name}"}
            else:
                queries_used.append(str(args.get("query", "")))
                try:
                    result = skill.function(**args)
                    payload = {"result": result}
                except Exception as exc:
                    logger.warning("Tool call failed for %s(%s): %s", call.name, args, exc)
                    payload = {"error": str(exc)}

            function_response_parts.append(
                types.Part.from_function_response(name=call.name, response=payload)
            )

        contents.append(types.Content(role="user", parts=function_response_parts))
    else:
        logger.warning(
            "Research agent hit research_agent_max_turns=%d without concluding naturally for %s",
            settings.research_agent_max_turns,
            ticker,
        )

    if not concluded_naturally:
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text="You're out of search turns. Conclude now with your best available findings."
                    )
                ],
            )
        )

    return _extract_structured_findings(client, contents, queries_used)


def _extract_structured_findings(
    client: genai.Client,
    contents: list[types.Content],
    queries_used: list[str],
    max_attempts: int = 2,
) -> SentimentFindings:
    """Phase B: force a clean, schema-constrained JSON summary out of the accumulated conversation."""
    schema = _sentiment_response_json_schema()
    extraction_contents = list(contents) + [
        types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        "Based on everything you found above, output your final sentiment "
                        "assessment now as JSON matching the required schema. Every entry in "
                        "key_catalysts and key_risks must use a source_url you actually retrieved."
                    )
                )
            ],
        )
    ]

    last_error: ValidationError | None = None

    for attempt in range(1, max_attempts + 1):
        response = generate_with_retry(
            client,
            model=settings.gemini_model,
            contents=extraction_contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )

        try:
            findings = SentimentFindings.model_validate_json(response.text)
            findings.queries_used = queries_used
            return findings
        except ValidationError as exc:
            last_error = exc
            logger.warning("Attempt %d/%d: invalid SentimentFindings JSON: %s", attempt, max_attempts, exc)
            extraction_contents = extraction_contents + [
                types.Content(role="model", parts=[types.Part(text=response.text or "")]),
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=f"That JSON was invalid: {exc}. Output corrected JSON matching the schema exactly."
                        )
                    ],
                ),
            ]

    logger.error("Research agent failed to produce valid SentimentFindings after %d attempts", max_attempts)
    raise last_error
