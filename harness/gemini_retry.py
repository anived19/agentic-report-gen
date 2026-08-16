"""
Thin retry wrapper around google-genai's generate_content.

The free tier (and low-quota paid tier) caps requests per minute per model.
When a 429 RESOURCE_EXHAUSTED is returned the API includes a retryDelay in
the error details; we parse it and wait accordingly before trying again.

Usage:
    from harness.gemini_retry import generate_with_retry

    response = generate_with_retry(client, model=..., contents=..., config=...)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)

# Fallback wait time (seconds) if the retry delay can't be parsed from the error
_DEFAULT_RETRY_WAIT = 30
# Maximum number of retry attempts before giving up
_MAX_RETRIES = 6


def _parse_retry_delay(exc: genai_errors.ClientError) -> float:
    """
    Try to extract the suggested retryDelay (e.g. '20s' or '20.47s') from the
    error payload.  Falls back to _DEFAULT_RETRY_WAIT if not found.

    Uses exc.details (the stored response_json dict set by APIError.__init__)
    rather than exc.args indexing, which is fragile across SDK versions.
    """
    try:
        # exc.details is set by APIError.__init__ as the raw response_json dict
        raw = getattr(exc, "details", {}) or {}
        details = raw.get("error", raw).get("details", [])
        for detail in details:
            delay = detail.get("retryDelay", "")
            if delay:
                # e.g. "20.472594942s" -> 20.47
                match = re.match(r"([\d.]+)s", str(delay))
                if match:
                    return float(match.group(1))
    except Exception:
        pass
    return _DEFAULT_RETRY_WAIT


def generate_with_retry(client: Any, *, model: str, contents: Any, config: Any = None) -> Any:
    """
    Call client.models.generate_content with automatic retry on 429.

    Args:
        client: A google.genai.Client instance.
        model:    Model name string (e.g. 'gemini-2.0-flash').
        contents: Contents list passed straight through.
        config:   GenerateContentConfig or None.

    Returns:
        The GenerateContentResponse on success.

    Raises:
        genai_errors.ClientError if all retries are exhausted or the error is
        not a 429.
    """
    kwargs: dict[str, Any] = {"model": model, "contents": contents}
    if config is not None:
        kwargs["config"] = config

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.ClientError as exc:
            if exc.code != 429 or attempt == _MAX_RETRIES:
                raise
            wait = _parse_retry_delay(exc) + 2  # small buffer on top
            logger.warning(
                "429 rate-limit hit (attempt %d/%d) — waiting %.1fs before retry …",
                attempt,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait)

    # Should never reach here, but satisfies type checkers
    raise RuntimeError("generate_with_retry exhausted all attempts")
