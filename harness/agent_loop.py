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
from schemas import SentimentFindings

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


def _extract_structured_findings(
    client: genai.Client,
    contents: list[types.Content],
    queries_used: list[str],
    max_attempts: int = 2,
) -> SentimentFindings:
    """Phase B: force a clean, schema-constrained JSON summary out of the accumulated conversation."""
    schema = _sentiment_response_json_schema()
    if contents and contents[0].role != "user":
        extraction_contents = [
            types.Content(role="user", parts=[types.Part(text="Perform research and sentiment extraction on the following search results.")])
        ] + list(contents)
    else:
        extraction_contents = list(contents)

    extraction_contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        "Based on everything you found above, output your final sentiment "
                        "assessment now as JSON matching the required schema. Every entry in "
                        "key_catalysts and key_risks must use a source_url you actually retrieved.\n"
                        "CRITICAL CURRENCY INSTRUCTION: Use the target company's native reporting currency. "
                        "For Indian companies (e.g. .NS, .BO, Tata, TCS, Infosys, Reliance), use Rs. / INR / Cr / Lakhs — "
                        "NEVER substitute USD '$' or '$ billion' for Indian rupee values unless the source explicitly discusses USD amounts."
                    )
                )
            ],
        )
    )

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
