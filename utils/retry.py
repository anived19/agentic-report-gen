"""
Small retry decorator for transient failures against external APIs
(yfinance and Tavily both raise assorted, mostly-untyped exceptions on
rate limits / flaky connections — this buys real reliability for ~10 lines).
"""
from __future__ import annotations

import functools
import logging
from typing import Callable, TypeVar

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)


def retry_on_transient_error(max_attempts: int = 3) -> Callable[[F], F]:
    """Retry with exponential backoff (1s, 2s, 4s...) on any Exception."""

    def decorator(func: F) -> F:
        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator
