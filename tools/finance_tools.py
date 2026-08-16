"""
Deterministic market data fetchers — wraps yfinance.

Design note: Each granular tool is independently callable and wrapped as a skill,
returning structured, validated numeric data. No LLM ever generates or alters these numbers.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd
import yfinance as yf

from config import settings
from schemas import MarketMetrics, PricePoint, QuarterlyDataPoint, format_currency_amount
from utils.retry import retry_on_transient_error

logger = logging.getLogger(__name__)

# Field partition maps (Maps schema field names -> tuple of yfinance .info keys)
_PRICE_INFO_FIELDS: dict[str, tuple[str, ...]] = {
    "company_name":    ("shortName", "longName"),
    "currency":        ("currency",),
    "sector":          ("sector",),
    "industry":        ("industry",),
    "current_price":   ("currentPrice", "regularMarketPrice"),
    "market_cap":      ("marketCap",),
}

_VALUATION_INFO_FIELDS: dict[str, tuple[str, ...]] = {
    "pe_ratio":        ("trailingPE",),
    "forward_pe":      ("forwardPE",),
    "pb_ratio":        ("priceToBook",),
    "ps_ratio":        ("priceToSalesTrailing12Months",),
    "ev_ebitda":       ("enterpriseToEbitda",),
    "dividend_yield":  ("dividendYield",),
    "revenue_ttm":     ("totalRevenue",),
    "gross_margin":    ("grossMargins",),
    "operating_margin":("operatingMargins",),
}

_FUNDAMENTALS_INFO_FIELDS: dict[str, tuple[str, ...]] = {
    "eps_ttm":         ("trailingEps",),
    "debt_to_equity":  ("debtToEquity",),
    "roe":             ("returnOnEquity",),
    "analyst_buy_count":     ("numberOfBuyAnalysts", "recommendationMeanBuy"),
    "analyst_hold_count":    ("numberOfHoldAnalysts",),
    "analyst_sell_count":    ("numberOfSellAnalysts",),
    "analyst_target_mean":   ("targetMeanPrice",),
    "analyst_target_high":   ("targetHighPrice",),
    "analyst_target_low":    ("targetLowPrice",),
    "analyst_recommendation":("recommendationKey",),
}

_INFO_FIELDS: dict[str, tuple[str, ...]] = {
    **_PRICE_INFO_FIELDS,
    **_VALUATION_INFO_FIELDS,
    **_FUNDAMENTALS_INFO_FIELDS,
}

_TRADING_DAYS_PER_MONTH = 21


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------

def _compute_rsi(closes: pd.Series, period: int = 14) -> float | None:
    """Compute RSI-{period} from a closing-price series. Returns None if not enough data."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean().iloc[-1]
    avg_loss = loss.rolling(window=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(float(100 - (100 / (1 + rs))), 2)


def _compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _compute_macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float | None, float | None, float | None]:
    """Returns (macd_line, signal_line, histogram) or (None, None, None)."""
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = _compute_ema(closes, fast)
    ema_slow = _compute_ema(closes, slow)
    macd = ema_fast - ema_slow
    sig = _compute_ema(macd, signal)
    hist = macd - sig
    return (
        round(float(macd.iloc[-1]), 4),
        round(float(sig.iloc[-1]), 4),
        round(float(hist.iloc[-1]), 4),
    )


def _volume_trend(volumes: pd.Series, short_window: int = 20, long_window: int = 60) -> str | None:
    """Compare recent average volume to longer-term average. Returns 'rising'/'falling'/'flat'."""
    if len(volumes) < long_window:
        return None
    short_avg = float(volumes.tail(short_window).mean())
    long_avg = float(volumes.tail(long_window).mean())
    if long_avg == 0:
        return None
    ratio = short_avg / long_avg
    if ratio > 1.10:
        return "rising"
    if ratio < 0.90:
        return "falling"
    return "flat"


# ---------------------------------------------------------------------------
# Quarterly financials helper
# ---------------------------------------------------------------------------

def _build_quarterly_financials(t: yf.Ticker) -> list[QuarterlyDataPoint]:
    """
    Extract last 4 quarters of revenue and net income, compute QoQ growth.
    Returns an empty list if data isn't available — never fabricates numbers.
    """
    try:
        qfin = t.quarterly_financials
        if qfin is None or qfin.empty:
            return []

        row_map = {str(idx).strip().lower(): idx for idx in qfin.index}

        revenue_candidates = [
            "total revenue", "totalrevenue", "operating revenue", "net revenues",
        ]
        income_candidates = [
            "net income", "netincome",
            "net income common stockholders",
            "net income from continuing operation net minority interest",
            "net income continuous operations",
        ]

        rev_key = next((row_map[k] for k in revenue_candidates if k in row_map), None)
        inc_key = next((row_map[k] for k in income_candidates if k in row_map), None)

        rev_row = qfin.loc[rev_key] if rev_key is not None else None
        inc_row = qfin.loc[inc_key] if inc_key is not None else None

        cols = list(qfin.columns[:4])
        if not cols:
            return []

        cols_oldest_first = list(reversed(cols))

        quarters: list[QuarterlyDataPoint] = []
        for i, col in enumerate(cols_oldest_first):
            label = "Q%d FY%d" % (col.quarter, col.year)

            def _safe_float(row, c) -> float | None:
                if row is None:
                    return None
                try:
                    v = row.get(c) if hasattr(row, "get") else row[c]
                    return float(v) if v is not None and not (isinstance(v, float) and (v != v)) else None
                except Exception:
                    return None

            rev = _safe_float(rev_row, col)
            inc = _safe_float(inc_row, col)

            rev_qoq = prof_qoq = None

            if i > 0:
                prev = quarters[i - 1]
                if rev is not None and prev.revenue is not None and prev.revenue != 0:
                    rev_qoq = round((rev - prev.revenue) / abs(prev.revenue) * 100, 2)
                if inc is not None and prev.net_income is not None and prev.net_income != 0:
                    prof_qoq = round((inc - prev.net_income) / abs(prev.net_income) * 100, 2)

            quarters.append(QuarterlyDataPoint(
                quarter=label,
                revenue=rev,
                net_income=inc,
                revenue_growth_qoq=rev_qoq,
                revenue_growth_yoy=None,
                profit_growth_qoq=prof_qoq,
                profit_growth_yoy=None,
            ))

        return list(reversed(quarters))

    except Exception as exc:
        logger.warning("Quarterly financials extraction failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Holdings helper
# ---------------------------------------------------------------------------

def _extract_holdings(t: yf.Ticker) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        "promoter_holding_pct": None,
        "fii_holding_pct": None,
        "dii_holding_pct": None,
        "public_holding_pct": None,
    }
    try:
        holders = t.major_holders
        if holders is None or holders.empty:
            return result

        def _to_pct(raw) -> float | None:
            try:
                s = str(raw).replace("%", "").strip()
                v = float(s)
                if 0 < v < 1:
                    v = round(v * 100, 2)
                return round(v, 2)
            except (ValueError, TypeError):
                return None

        idx_lower = {str(i).lower(): i for i in holders.index}

        insider_key = next(
            (idx_lower[k] for k in idx_lower
             if "insider" in k and "percent" in k), None
        )
        inst_key = next(
            (idx_lower[k] for k in idx_lower
             if "institution" in k and "percent" in k
             and "float" not in k), None
        )

        if insider_key is not None:
            raw = holders.loc[insider_key].iloc[0]
            result["promoter_holding_pct"] = _to_pct(raw)

        if inst_key is not None:
            raw = holders.loc[inst_key].iloc[0]
            result["fii_holding_pct"] = _to_pct(raw)

        if result["promoter_holding_pct"] is None and len(holders.columns) >= 2:
            val_col = holders.columns[0]
            lbl_col = holders.columns[1]
            rows = {str(row[lbl_col]).strip().lower(): row[val_col]
                    for _, row in holders.iterrows()}

            for key in ("% of shares held by all insider", "insiderpercent"):
                if key in rows:
                    result["promoter_holding_pct"] = _to_pct(rows[key])
                    break

            if result["fii_holding_pct"] is None:
                for key in ("% of shares held by institutions", "institutionpercent"):
                    if key in rows:
                        result["fii_holding_pct"] = _to_pct(rows[key])
                        break

        if result["promoter_holding_pct"] is not None and result["fii_holding_pct"] is not None:
            residual = 100.0 - result["promoter_holding_pct"] - result["fii_holding_pct"]
            result["public_holding_pct"] = round(max(residual, 0.0), 2)

    except Exception as exc:
        logger.warning("Holdings extraction failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# ROCE helper
# ---------------------------------------------------------------------------

def _compute_roce(info: dict) -> float | None:
    try:
        ebit = info.get("ebit")
        total_assets = info.get("totalAssets")
        current_liabilities = info.get("currentLiabilities") or info.get("totalCurrentLiabilities")
        if ebit is not None and total_assets is not None and current_liabilities is not None:
            capital_employed = total_assets - current_liabilities
            if capital_employed > 0:
                return round(float(ebit) / float(capital_employed), 4)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Granular Fetch Functions (Skill-callable)
# ---------------------------------------------------------------------------

@retry_on_transient_error(max_attempts=3)
def get_price_snapshot(ticker: str) -> dict[str, Any]:
    """Fetch current price, market cap, moving averages (50d/200d), and outlook high/low."""
    t = yf.Ticker(ticker)
    info = {}
    try:
        info = t.info or {}
    except Exception as exc:
        logger.warning("get_price_snapshot .info failed for %s: %s", ticker, exc)

    res: dict[str, Any] = {
        "ticker": ticker,
        "company_name": next((info[k] for k in ("shortName", "longName") if info.get(k) is not None), None),
        "currency": info.get("currency"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "current_price": next((info[k] for k in ("currentPrice", "regularMarketPrice") if info.get(k) is not None), None),
        "market_cap": info.get("marketCap"),
        "market_cap_formatted": format_currency_amount(info.get("marketCap"), info.get("currency")),
        "fifty_day_ma": None,
        "two_hundred_day_ma": None,
        "outlook_high": None,
        "outlook_low": None,
        "outlook_price_trend": [],
    }

    outlook_trading_days = settings.outlook_months * _TRADING_DAYS_PER_MONTH
    try:
        hist = t.history(period="1y", interval="1d", auto_adjust=False)
        if hist is not None and not hist.empty:
            closes = hist["Close"].dropna()
            if len(closes) >= 50:
                res["fifty_day_ma"] = round(float(closes.tail(50).mean()), 2)
            if len(closes) >= 200:
                res["two_hundred_day_ma"] = round(float(closes.tail(200).mean()), 2)
            outlook_closes = closes.tail(outlook_trading_days)
            if not outlook_closes.empty:
                res["outlook_high"] = round(float(outlook_closes.max()), 2)
                res["outlook_low"] = round(float(outlook_closes.min()), 2)
                res["outlook_price_trend"] = [
                    {"date": idx.date().isoformat(), "close": round(float(v), 2)}
                    for idx, v in outlook_closes.items()
                ]
    except Exception as exc:
        logger.warning("get_price_snapshot .history failed for %s: %s", ticker, exc)

    return res


@retry_on_transient_error(max_attempts=3)
def get_valuation_multiples(ticker: str) -> dict[str, Any]:
    """Fetch valuation multiples: P/E, forward P/E, P/B, P/S, EV/EBITDA, dividend yield, and margins."""
    t = yf.Ticker(ticker)
    info = {}
    try:
        info = t.info or {}
    except Exception as exc:
        logger.warning("get_valuation_multiples .info failed for %s: %s", ticker, exc)

    currency = info.get("currency")
    revenue_ttm = info.get("totalRevenue")
    return {
        "pe_ratio": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "pb_ratio": info.get("priceToBook"),
        "ps_ratio": info.get("priceToSalesTrailing12Months"),
        "ev_ebitda": info.get("enterpriseToEbitda"),
        "dividend_yield": info.get("dividendYield"),
        "revenue_ttm": revenue_ttm,
        "revenue_ttm_formatted": format_currency_amount(revenue_ttm, currency),
        "gross_margin": info.get("grossMargins"),
        "operating_margin": info.get("operatingMargins"),
    }


@retry_on_transient_error(max_attempts=3)
def get_fundamentals(ticker: str) -> dict[str, Any]:
    """Fetch EPS, debt-to-equity, ROE, ROCE, and broker analyst consensus."""
    t = yf.Ticker(ticker)
    info = {}
    try:
        info = t.info or {}
    except Exception as exc:
        logger.warning("get_fundamentals .info failed for %s: %s", ticker, exc)

    return {
        "eps_ttm": info.get("trailingEps"),
        "debt_to_equity": info.get("debtToEquity"),
        "roe": info.get("returnOnEquity"),
        "roce": _compute_roce(info),
        "analyst_buy_count": next((info[k] for k in ("numberOfBuyAnalysts", "recommendationMeanBuy") if info.get(k) is not None), None),
        "analyst_hold_count": info.get("numberOfHoldAnalysts"),
        "analyst_sell_count": info.get("numberOfSellAnalysts"),
        "analyst_target_mean": info.get("targetMeanPrice"),
        "analyst_target_high": info.get("targetHighPrice"),
        "analyst_target_low": info.get("targetLowPrice"),
        "analyst_recommendation": info.get("recommendationKey"),
    }


@retry_on_transient_error(max_attempts=3)
def get_quarterly_financials(ticker: str) -> list[dict[str, Any]]:
    """Fetch quarterly financials (revenue, net income, QoQ growth) for the last 4 quarters."""
    t = yf.Ticker(ticker)
    data = _build_quarterly_financials(t)
    return [d.model_dump() for d in data]


@retry_on_transient_error(max_attempts=3)
def get_technicals(ticker: str) -> dict[str, Any]:
    """Fetch technical analysis metrics: RSI-14, MACD, volume trend, and support/resistance."""
    t = yf.Ticker(ticker)
    res: dict[str, Any] = {
        "rsi_14": None,
        "macd_line": None,
        "macd_signal": None,
        "macd_histogram": None,
        "volume_20d_avg": None,
        "volume_trend": None,
        "support_level": None,
        "resistance_level": None,
    }
    outlook_trading_days = settings.outlook_months * _TRADING_DAYS_PER_MONTH
    try:
        hist = t.history(period="1y", interval="1d", auto_adjust=False)
        if hist is not None and not hist.empty:
            closes = hist["Close"].dropna()
            volumes = hist["Volume"].dropna() if "Volume" in hist.columns else pd.Series(dtype=float)
            res["rsi_14"] = _compute_rsi(closes, period=14)
            macd_line, macd_signal, macd_hist = _compute_macd(closes)
            res["macd_line"] = macd_line
            res["macd_signal"] = macd_signal
            res["macd_histogram"] = macd_hist
            if not volumes.empty:
                res["volume_20d_avg"] = round(float(volumes.tail(20).mean()), 0)
                res["volume_trend"] = _volume_trend(volumes)
            outlook_closes = closes.tail(outlook_trading_days)
            if not outlook_closes.empty:
                res["support_level"] = round(float(outlook_closes.quantile(0.10)), 2)
                res["resistance_level"] = round(float(outlook_closes.quantile(0.90)), 2)
    except Exception as exc:
        logger.warning("get_technicals failed for %s: %s", ticker, exc)
    return res


@retry_on_transient_error(max_attempts=3)
def get_ownership(ticker: str) -> dict[str, Any]:
    """Fetch promoter, institutional, and public holding percentages."""
    t = yf.Ticker(ticker)
    return _extract_holdings(t)


# ---------------------------------------------------------------------------
# Assembly helper & Legacy Wrapper
# ---------------------------------------------------------------------------

def assemble_market_metrics(ticker: str, data: dict[str, Any]) -> MarketMetrics:
    """
    Assemble a MarketMetrics Pydantic object from granular dictionary data.
    Automatically checks and populates unavailable_fields.
    """
    unavailable = []
    
    # Parse outlook price trend
    trend = []
    raw_trend = data.get("outlook_price_trend") or []
    for pt in raw_trend:
        if isinstance(pt, dict) and "date" in pt and "close" in pt:
            try:
                d = date.fromisoformat(pt["date"]) if isinstance(pt["date"], str) else pt["date"]
                trend.append(PricePoint(date=d, close=float(pt["close"])))
            except Exception:
                pass
        elif isinstance(pt, PricePoint):
            trend.append(pt)

    # Parse quarterly financials
    quarterly = []
    raw_qfin = data.get("quarterly_financials") or []
    for qf in raw_qfin:
        if isinstance(qf, dict):
            try:
                quarterly.append(QuarterlyDataPoint.model_validate(qf))
            except Exception:
                pass
        elif isinstance(qf, QuarterlyDataPoint):
            quarterly.append(qf)

    # Check key field availability
    field_keys = [
        "company_name", "currency", "current_price", "market_cap",
        "fifty_day_ma", "two_hundred_day_ma", "rsi_14", "macd_line",
        "macd_signal", "macd_histogram", "volume_20d_avg", "volume_trend",
        "support_level", "resistance_level", "pe_ratio", "forward_pe",
        "pb_ratio", "ps_ratio", "ev_ebitda", "dividend_yield", "eps_ttm",
        "revenue_ttm", "gross_margin", "operating_margin", "debt_to_equity",
        "roe", "roce", "analyst_buy_count", "analyst_target_mean",
        "promoter_holding_pct", "fii_holding_pct"
    ]
    for k in field_keys:
        if data.get(k) is None:
            unavailable.append(k)

    if not quarterly:
        unavailable.append("quarterly_financials")

    return MarketMetrics(
        ticker=ticker,
        company_name=data.get("company_name"),
        currency=data.get("currency"),
        sector=data.get("sector"),
        industry=data.get("industry"),
        current_price=data.get("current_price"),
        fifty_day_ma=data.get("fifty_day_ma"),
        two_hundred_day_ma=data.get("two_hundred_day_ma"),
        rsi_14=data.get("rsi_14"),
        macd_line=data.get("macd_line"),
        macd_signal=data.get("macd_signal"),
        macd_histogram=data.get("macd_histogram"),
        volume_20d_avg=data.get("volume_20d_avg"),
        volume_trend=data.get("volume_trend"),
        support_level=data.get("support_level"),
        resistance_level=data.get("resistance_level"),
        market_cap=data.get("market_cap"),
        market_cap_formatted=data.get("market_cap_formatted") or format_currency_amount(data.get("market_cap"), data.get("currency")),
        pe_ratio=data.get("pe_ratio"),
        forward_pe=data.get("forward_pe"),
        pb_ratio=data.get("pb_ratio"),
        ps_ratio=data.get("ps_ratio"),
        ev_ebitda=data.get("ev_ebitda"),
        dividend_yield=data.get("dividend_yield"),
        eps_ttm=data.get("eps_ttm"),
        revenue_ttm=data.get("revenue_ttm"),
        revenue_ttm_formatted=data.get("revenue_ttm_formatted") or format_currency_amount(data.get("revenue_ttm"), data.get("currency")),
        gross_margin=data.get("gross_margin"),
        operating_margin=data.get("operating_margin"),
        debt_to_equity=data.get("debt_to_equity"),
        roe=data.get("roe"),
        roce=data.get("roce"),
        analyst_buy_count=data.get("analyst_buy_count"),
        analyst_hold_count=data.get("analyst_hold_count"),
        analyst_sell_count=data.get("analyst_sell_count"),
        analyst_target_mean=data.get("analyst_target_mean"),
        analyst_target_high=data.get("analyst_target_high"),
        analyst_target_low=data.get("analyst_target_low"),
        analyst_recommendation=data.get("analyst_recommendation"),
        promoter_holding_pct=data.get("promoter_holding_pct"),
        fii_holding_pct=data.get("fii_holding_pct"),
        dii_holding_pct=data.get("dii_holding_pct"),
        public_holding_pct=data.get("public_holding_pct"),
        quarterly_financials=quarterly,
        outlook_months=settings.outlook_months,
        outlook_high=data.get("outlook_high"),
        outlook_low=data.get("outlook_low"),
        outlook_price_trend=trend,
        unavailable_fields=unavailable,
        fetched_at=date.today(),
    )


@retry_on_transient_error(max_attempts=3)
def fetch_yfinance_data(ticker: str) -> MarketMetrics:
    """Fetch complete MarketMetrics by executing granular fetchers."""
    merged: dict[str, Any] = {}
    merged.update(get_price_snapshot(ticker))
    merged.update(get_valuation_multiples(ticker))
    merged.update(get_fundamentals(ticker))
    merged.update(get_technicals(ticker))
    merged.update(get_ownership(ticker))
    merged["quarterly_financials"] = get_quarterly_financials(ticker)
    return assemble_market_metrics(ticker, merged)
