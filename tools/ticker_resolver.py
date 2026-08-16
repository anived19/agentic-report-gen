"""
Resolves a natural-language company reference (e.g. "Reliance Industries", "Tata")
to validated candidate ticker symbols.

Resolution order:
  1. Conglomerate map (tools/conglomerate_map.yaml) — detects multi-entity group queries.
  2. Static lookup table — fast, zero extra API calls for exact known names.
  3. yfinance search (yf.Search) with candidate filtering and ticker validation.

Zero-candidate fail-closed rule:
  If resolve_entity finds 0 candidates, it retries once with a broadened/cleaned query.
  If still 0 candidates, resolution fails explicitly rather than guessing a symbol.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
import yfinance as yf

from schemas import TickerResolution

logger = logging.getLogger(__name__)

_CONGLOMERATE_MAP_PATH = Path("tools/conglomerate_map.yaml")

def _load_conglomerate_map() -> dict[str, list[dict[str, Any]]]:
    if _CONGLOMERATE_MAP_PATH.exists():
        try:
            with open(_CONGLOMERATE_MAP_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return {k.strip().lower(): v for k, v in data.items() if isinstance(v, list)}
        except Exception as exc:
            logger.warning("Could not load conglomerate_map.yaml: %s", exc)
    return {}

_CONGLOMERATE_MAP = _load_conglomerate_map()

# Static map for unambiguous common names
_STATIC_MAP: dict[str, str] = {
    "reliance industries": "RELIANCE.NS",
    "tata consultancy services": "TCS.NS",
    "tcs": "TCS.NS",
    "infosys": "INFY.NS",
    "hdfc bank": "HDFCBANK.NS",
    "icici bank": "ICICIBANK.NS",
    "state bank of india": "SBIN.NS",
    "sbi": "SBIN.NS",
    "wipro": "WIPRO.NS",
    "tata motors": "TMPV.NS",
    "tata motors passenger vehicles": "TMPV.NS",
    "tata motors commercial vehicles": "TMCV.NS",
    "bharti airtel": "BHARTIARTL.NS",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "amazon": "AMZN",
    "tesla": "TSLA",
    "nvidia": "NVDA",
    "meta": "META",
}


def _validate_ticker(ticker: str) -> bool:
    """Confirm yfinance actually returns data for this symbol before trusting it."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        return not hist.empty
    except Exception as exc:
        logger.warning("Ticker validation failed for %s: %s", ticker, exc)
        return False


def _clean_query(query: str) -> str:
    """Strip common boilerplate conversational words and punctuation."""
    q = query.strip().lower()
    for phrase in (
        "give me a stock report of",
        "give me stock report of",
        "stock report of",
        "valuation report of",
        "sentiment report of",
        "full equity report on",
        "report of",
        "report on",
        "stock of",
        "shares of",
        "analysis of",
        "group",
        "stock",
        "shares",
        "company",
    ):
        q = q.replace(phrase, " ")
    q = re.sub(r"[^\w\s]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def resolve_entity(query: str) -> list[dict[str, Any]]:
    """
    Resolve a query into a list of candidate entity dicts:
    [{"ticker": str, "name": str, "exchange": str, "sector": str, "confidence": float}]
    """
    raw_norm = query.strip().lower()
    cleaned = _clean_query(query)

    # 1. Check for specific known company names in static map first
    for candidate_name, ticker in _STATIC_MAP.items():
        if candidate_name == raw_norm or candidate_name == cleaned:
            if _validate_ticker(ticker):
                return [{
                    "ticker": ticker,
                    "name": candidate_name.title(),
                    "exchange": "NSE" if ticker.endswith(".NS") else "US",
                    "sector": "General",
                    "confidence": 1.0,
                }]

    # 2. Conglomerate map check
    # Check if cleaned query or raw query targets a conglomerate group
    for group_name, candidates in _CONGLOMERATE_MAP.items():
        # Match if cleaned query is exactly the group name (e.g. 'tata', 'adani')
        # or if the word appears as a standalone token without a more specific company
        tokens = set(cleaned.split())
        if cleaned == group_name or group_name in tokens or raw_norm == group_name or f"{group_name} group" in raw_norm:
            valid_candidates = []
            for c in candidates:
                if _validate_ticker(c["ticker"]):
                    valid_candidates.append(dict(c))
            if valid_candidates:
                return valid_candidates

    # 3. yfinance search with candidate deduplication and filtering
    seen_tickers: set[str] = set()

    def _is_derivative_or_junk(sym: str, quote: dict[str, Any]) -> bool:
        """Filter out futures, options, currency pairs, and derivatives."""
        s = sym.upper()
        if any(bad in s for bad in ("-", ".SI", "=X", "^", "FUT", ".FUT", "FUTURES")):
            return True
        q_type = str(quote.get("quoteType", "")).upper()
        if q_type in ("FUTURE", "OPTION", "CURRENCY", "INDEX"):
            return True
        return False

    def _search_pass(search_term: str) -> list[dict[str, Any]]:
        found = []
        try:
            results = yf.Search(search_term, max_results=10).quotes
        except Exception as exc:
            logger.warning("yfinance search failed for %r: %s", search_term, exc)
            results = []

        # First collect all NSE symbols from results to suppress duplicate BSE symbols
        nse_bases = set()
        for r in results:
            sym = r.get("symbol") or ""
            if sym.endswith(".NS"):
                nse_bases.add(sym[:-3].upper())

        for r in results:
            sym = r.get("symbol")
            if not sym or sym in seen_tickers:
                continue

            if _is_derivative_or_junk(sym, r):
                continue

            # If this is a BSE quote (e.g. TMCV.BO) and an NSE quote (TMCV.NS) exists, skip BSE
            if sym.endswith(".BO") and sym[:-3].upper() in nse_bases:
                continue

            name = r.get("shortname") or r.get("longname") or sym
            exch = r.get("exchange") or ("NSE" if sym.endswith(".NS") else ("BSE" if sym.endswith(".BO") else ""))
            sector = r.get("sector") or r.get("industry") or "General"

            if _validate_ticker(sym):
                seen_tickers.add(sym)
                found.append({
                    "ticker": sym,
                    "name": name,
                    "exchange": exch,
                    "sector": sector,
                    "confidence": 0.8 if sym.upper() == search_term.upper() else 0.6,
                })
        return found

    candidates = _search_pass(cleaned or query)

    # 4. If 0 candidates, retry once with raw query if cleaned was different
    if not candidates and cleaned and cleaned != raw_norm:
        logger.info("Retrying entity resolution with raw query: %r", query)
        candidates = _search_pass(raw_norm)

    return candidates


def resolve_ticker(query: str) -> TickerResolution:
    """Legacy helper returning TickerResolution for single-ticker waterfall compatibility."""
    candidates = resolve_entity(query)
    if not candidates:
        return TickerResolution(query=query, resolved_ticker=None, confidence=0.0, method="unresolved")

    first = candidates[0]
    return TickerResolution(
        query=query,
        resolved_ticker=first["ticker"],
        confidence=first.get("confidence", 0.7),
        method="resolve_entity",
    )
