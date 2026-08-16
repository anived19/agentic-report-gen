"""
Deterministic market data fetchers — wraps yfinance.

Design note: Each granular tool is independently callable and wrapped as a skill,
returning structured, validated numeric data. No LLM ever generates or alters these numbers.
"""
from __future__ import annotations

import ast
import logging
import math
from datetime import date
from typing import Any

import pandas as pd
import yfinance as yf

from config import settings
from schemas import (
    MarketMetrics,
    PricePoint,
    QuarterlyDataPoint,
    format_currency_amount,
    format_number_amount,
    format_percent,
)
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

        currency = None
        try:
            currency = t.info.get("currency")
        except Exception:
            pass

        # Explicitly sort columns descending by date to ensure newest-to-oldest ordering
        cols = sorted(qfin.columns, reverse=True)[:4]
        if not cols:
            return []

        cols_oldest_first = list(reversed(cols))

        quarters: list[QuarterlyDataPoint] = []
        for i, col in enumerate(cols_oldest_first):
            label = "Q%d FY%d" % (col.quarter, col.year)
            data_gap_note = None

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

            # Mathematical Sanity Check: Net Income MUST NEVER exceed Total Revenue for the same period
            if rev is not None and inc is not None and inc > rev:
                logger.warning(
                    "Quarterly sanity check failed for %s: Net Income (%f) > Revenue (%f). Flagging anomalous metric.",
                    label, inc, rev,
                )
                data_gap_note = "Data anomaly detected: Net Income exceeds Revenue for this period (metric omitted)."
                inc = None

            rev_qoq = prof_qoq = None
            rev_qoq_fmt = prof_qoq_fmt = None

            if i > 0:
                prev = quarters[i - 1]
                prev_col = cols_oldest_first[i - 1]

                # Continuity check: verify period delta is ~3 months (80-100 days)
                try:
                    ts_curr = pd.to_datetime(col)
                    ts_prev = pd.to_datetime(prev_col)
                    delta_days = (ts_curr - ts_prev).days
                    if delta_days > 100:
                        gap_months = round(delta_days / 30.44)
                        logger.warning(
                            "Quarterly financials gap detected between %s (%s) and %s (%s): ~%d months (%d days, expected ~3 months)",
                            prev.quarter, ts_prev.strftime('%Y-%m-%d'), label, ts_curr.strftime('%Y-%m-%d'), gap_months, delta_days
                        )
                        data_gap_note = "A prior quarter may be missing from source data (yfinance)."
                except Exception as gap_exc:
                    logger.debug("Quarterly date delta check failed: %s", gap_exc)

                if data_gap_note is None:
                    if rev is not None and prev.revenue is not None and prev.revenue != 0:
                        rev_qoq = round((rev - prev.revenue) / abs(prev.revenue) * 100, 2)
                        rev_qoq_fmt = f"{rev_qoq:+.2f}%"
                    if inc is not None and prev.net_income is not None and prev.net_income != 0:
                        prof_qoq = round((inc - prev.net_income) / abs(prev.net_income) * 100, 2)
                        prof_qoq_fmt = f"{prof_qoq:+.2f}%"
                else:
                    # Do not compute non-sequential QoQ growth across a missing period gap
                    rev_qoq = None
                    prof_qoq = None
                    rev_qoq_fmt = "data unavailable"
                    prof_qoq_fmt = "data unavailable"
            else:
                rev_qoq_fmt = "data unavailable"
                prof_qoq_fmt = "data unavailable"

            rev_fmt = format_currency_amount(rev, currency) if rev is not None else None
            inc_fmt = format_currency_amount(inc, currency) if inc is not None else None

            quarters.append(QuarterlyDataPoint(
                quarter=label,
                revenue=rev,
                revenue_formatted=rev_fmt,
                net_income=inc,
                net_income_formatted=inc_fmt,
                revenue_growth_qoq=rev_qoq,
                revenue_growth_qoq_formatted=rev_qoq_fmt,
                revenue_growth_yoy=None,
                profit_growth_qoq=prof_qoq,
                profit_growth_qoq_formatted=prof_qoq_fmt,
                profit_growth_yoy=None,
                data_gap_note=data_gap_note,
            ))

        return list(reversed(quarters))

    except Exception as exc:
        logger.warning("Quarterly financials extraction failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Holdings helper
# ---------------------------------------------------------------------------

def _extract_holdings(t: yf.Ticker) -> dict[str, Any]:
    result: dict[str, Any] = {
        "promoter_holding_pct": None,
        "promoter_holding_pct_formatted": None,
        "fii_holding_pct": None,
        "institutional_holding_pct_formatted": None,
        "dii_holding_pct": None,
        "public_holding_pct": None,
        "public_holding_pct_formatted": None,
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

        if result["promoter_holding_pct"] is not None:
            result["promoter_holding_pct_formatted"] = format_percent(result["promoter_holding_pct"])
        if result["fii_holding_pct"] is not None:
            result["institutional_holding_pct_formatted"] = format_percent(result["fii_holding_pct"])
        if result["public_holding_pct"] is not None:
            result["public_holding_pct_formatted"] = format_percent(result["public_holding_pct"])

    except Exception as exc:
        logger.warning("Holdings extraction failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# ROCE helper
# ---------------------------------------------------------------------------

def _compute_roce(info: dict, t: yf.Ticker | None = None) -> float | None:
    try:
        ebit = None
        total_assets = None
        current_liabilities = None

        # 1. Try pulling operating income / EBIT and balance sheet items from statements
        if t is not None:
            for stmt in (t.income_stmt, t.quarterly_income_stmt):
                if stmt is not None and not stmt.empty:
                    idx_map = {str(i).strip().lower(): i for i in stmt.index}
                    for k in ("ebit", "operating income", "operating profit", "operatingincome"):
                        if k in idx_map:
                            val = stmt.loc[idx_map[k]].iloc[0]
                            if pd.notna(val):
                                ebit = float(val)
                                break
                    if ebit is not None:
                        break

            for bs in (t.balance_sheet, t.quarterly_balance_sheet):
                if bs is not None and not bs.empty:
                    idx_map = {str(i).strip().lower(): i for i in bs.index}
                    for k in ("total assets", "totalassets"):
                        if k in idx_map and total_assets is None:
                            val = bs.loc[idx_map[k]].iloc[0]
                            if pd.notna(val):
                                total_assets = float(val)
                    for k in ("current liabilities", "total current liabilities", "currentliabilities", "totalcurrentliabilities"):
                        if k in idx_map and current_liabilities is None:
                            val = bs.loc[idx_map[k]].iloc[0]
                            if pd.notna(val):
                                current_liabilities = float(val)
                    if total_assets is not None and current_liabilities is not None:
                        break

        # 2. Fall back to .info keys
        if ebit is None:
            ebit = info.get("ebit") or info.get("operatingIncome")
        if total_assets is None:
            total_assets = info.get("totalAssets")
        if current_liabilities is None:
            current_liabilities = info.get("currentLiabilities") or info.get("totalCurrentLiabilities")

        if ebit is not None and total_assets is not None and current_liabilities is not None:
            capital_employed = float(total_assets) - float(current_liabilities)
            if capital_employed > 0:
                return round(float(ebit) / float(capital_employed), 4)
    except Exception as exc:
        logger.debug("ROCE computation failed: %s", exc)
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

    curr_price = next((info[k] for k in ("currentPrice", "regularMarketPrice") if info.get(k) is not None), None)
    if curr_price is not None:
        curr_price = round(float(curr_price), 2)
    mcap = info.get("marketCap")
    if mcap is not None:
        mcap = round(float(mcap), 2)

    res: dict[str, Any] = {
        "ticker": ticker,
        "company_name": next((info[k] for k in ("shortName", "longName") if info.get(k) is not None), None),
        "currency": info.get("currency"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "current_price": curr_price,
        "current_price_formatted": format_number_amount(curr_price),
        "market_cap": mcap,
        "market_cap_formatted": format_currency_amount(mcap, info.get("currency")),
        "fifty_day_ma": None,
        "fifty_day_ma_formatted": None,
        "two_hundred_day_ma": None,
        "two_hundred_day_ma_formatted": None,
        "outlook_high": None,
        "outlook_high_formatted": None,
        "outlook_low": None,
        "outlook_low_formatted": None,
        "outlook_price_trend": [],
    }

    outlook_trading_days = settings.outlook_months * _TRADING_DAYS_PER_MONTH
    try:
        hist = t.history(period="1y", interval="1d", auto_adjust=False)
        if hist is not None and not hist.empty:
            closes = hist["Close"].dropna()
            if len(closes) >= 50:
                ma50 = round(float(closes.tail(50).mean()), 2)
                res["fifty_day_ma"] = ma50
                res["fifty_day_ma_formatted"] = format_number_amount(ma50)
            if len(closes) >= 200:
                ma200 = round(float(closes.tail(200).mean()), 2)
                res["two_hundred_day_ma"] = ma200
                res["two_hundred_day_ma_formatted"] = format_number_amount(ma200)
            outlook_closes = closes.tail(outlook_trading_days)
            if not outlook_closes.empty:
                ohigh = round(float(outlook_closes.max()), 2)
                olow = round(float(outlook_closes.min()), 2)
                res["outlook_high"] = ohigh
                res["outlook_high_formatted"] = format_number_amount(ohigh)
                res["outlook_low"] = olow
                res["outlook_low_formatted"] = format_number_amount(olow)
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
    if revenue_ttm is not None:
        revenue_ttm = round(float(revenue_ttm), 2)

    def _rnd(val, decimals=2):
        return round(float(val), decimals) if val is not None else None

    pe = _rnd(info.get("trailingPE"))
    fpe = _rnd(info.get("forwardPE"))
    pb = _rnd(info.get("priceToBook"))
    ps = _rnd(info.get("priceToSalesTrailing12Months"))
    ev = _rnd(info.get("enterpriseToEbitda"))
    dy = _rnd(info.get("dividendYield"), 4)
    gm = _rnd(info.get("grossMargins"), 4)
    om = _rnd(info.get("operatingMargins"), 4)

    return {
        "pe_ratio": pe,
        "pe_ratio_formatted": format_number_amount(pe),
        "forward_pe": fpe,
        "forward_pe_formatted": format_number_amount(fpe),
        "pb_ratio": pb,
        "pb_ratio_formatted": format_number_amount(pb),
        "ps_ratio": ps,
        "ps_ratio_formatted": format_number_amount(ps),
        "ev_ebitda": ev,
        "ev_ebitda_formatted": format_number_amount(ev),
        "dividend_yield": dy,
        "dividend_yield_formatted": format_percent(dy),
        "revenue_ttm": revenue_ttm,
        "revenue_ttm_formatted": format_currency_amount(revenue_ttm, currency),
        "gross_margin": gm,
        "gross_margin_formatted": format_percent(gm),
        "operating_margin": om,
        "operating_margin_formatted": format_percent(om),
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

    def _rnd(val, decimals=2):
        return round(float(val), decimals) if val is not None else None

    eps = _rnd(info.get("trailingEps"))
    dte = _rnd(info.get("debtToEquity"))
    roe = _rnd(info.get("returnOnEquity"), 4)
    roce = _compute_roce(info, t=t)

    # 1. Analyst recommendations count
    buy_count = None
    hold_count = None
    sell_count = None
    try:
        recs = t.recommendations
        if recs is None or recs.empty:
            recs = t.recommendations_summary
        if recs is not None and not recs.empty:
            row = recs.iloc[0]
            buy_count = int(row.get("strongBuy", 0) + row.get("buy", 0))
            hold_count = int(row.get("hold", 0))
            sell_count = int(row.get("sell", 0) + row.get("strongSell", 0))
    except Exception as exc:
        logger.debug("Recommendations accessor failed for %s: %s", ticker, exc)

    if buy_count is None:
        buy_count = next((info[k] for k in ("numberOfBuyAnalysts", "recommendationMeanBuy") if info.get(k) is not None), None)
    if hold_count is None:
        hold_count = info.get("numberOfHoldAnalysts")
    if sell_count is None:
        sell_count = info.get("numberOfSellAnalysts")

    # 2. Analyst price targets
    target_mean = None
    target_high = None
    target_low = None
    try:
        apt = t.analyst_price_targets
        if isinstance(apt, dict):
            if apt.get("mean") is not None:
                target_mean = round(float(apt["mean"]), 2)
            if apt.get("high") is not None:
                target_high = round(float(apt["high"]), 2)
            if apt.get("low") is not None:
                target_low = round(float(apt["low"]), 2)
    except Exception as exc:
        logger.debug("Analyst price targets accessor failed for %s: %s", ticker, exc)

    if target_mean is None and info.get("targetMeanPrice") is not None:
        target_mean = round(float(info["targetMeanPrice"]), 2)
    if target_high is None and info.get("targetHighPrice") is not None:
        target_high = round(float(info["targetHighPrice"]), 2)
    if target_low is None and info.get("targetLowPrice") is not None:
        target_low = round(float(info["targetLowPrice"]), 2)

    return {
        "eps_ttm": eps,
        "eps_ttm_formatted": format_number_amount(eps),
        "debt_to_equity": dte,
        "debt_to_equity_formatted": format_number_amount(dte),
        "roe": roe,
        "roe_formatted": format_percent(roe),
        "roce": roce,
        "roce_formatted": format_percent(roce),
        "analyst_buy_count": buy_count,
        "analyst_hold_count": hold_count,
        "analyst_sell_count": sell_count,
        "analyst_target_mean": target_mean,
        "analyst_target_mean_formatted": format_number_amount(target_mean),
        "analyst_target_high": target_high,
        "analyst_target_high_formatted": format_number_amount(target_high),
        "analyst_target_low": target_low,
        "analyst_target_low_formatted": format_number_amount(target_low),
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
        current_price_formatted=data.get("current_price_formatted") or format_number_amount(data.get("current_price")),
        fifty_day_ma=data.get("fifty_day_ma"),
        fifty_day_ma_formatted=data.get("fifty_day_ma_formatted") or format_number_amount(data.get("fifty_day_ma")),
        two_hundred_day_ma=data.get("two_hundred_day_ma"),
        two_hundred_day_ma_formatted=data.get("two_hundred_day_ma_formatted") or format_number_amount(data.get("two_hundred_day_ma")),
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
        pe_ratio_formatted=data.get("pe_ratio_formatted") or format_number_amount(data.get("pe_ratio")),
        forward_pe=data.get("forward_pe"),
        forward_pe_formatted=data.get("forward_pe_formatted") or format_number_amount(data.get("forward_pe")),
        pb_ratio=data.get("pb_ratio"),
        pb_ratio_formatted=data.get("pb_ratio_formatted") or format_number_amount(data.get("pb_ratio")),
        ps_ratio=data.get("ps_ratio"),
        ps_ratio_formatted=data.get("ps_ratio_formatted") or format_number_amount(data.get("ps_ratio")),
        ev_ebitda=data.get("ev_ebitda"),
        ev_ebitda_formatted=data.get("ev_ebitda_formatted") or format_number_amount(data.get("ev_ebitda")),
        dividend_yield=data.get("dividend_yield"),
        dividend_yield_formatted=data.get("dividend_yield_formatted") or format_percent(data.get("dividend_yield")),
        eps_ttm=data.get("eps_ttm"),
        eps_ttm_formatted=data.get("eps_ttm_formatted") or format_number_amount(data.get("eps_ttm")),
        revenue_ttm=data.get("revenue_ttm"),
        revenue_ttm_formatted=data.get("revenue_ttm_formatted") or format_currency_amount(data.get("revenue_ttm"), data.get("currency")),
        gross_margin=data.get("gross_margin"),
        gross_margin_formatted=data.get("gross_margin_formatted") or format_percent(data.get("gross_margin")),
        operating_margin=data.get("operating_margin"),
        operating_margin_formatted=data.get("operating_margin_formatted") or format_percent(data.get("operating_margin")),
        debt_to_equity=data.get("debt_to_equity"),
        debt_to_equity_formatted=data.get("debt_to_equity_formatted") or format_number_amount(data.get("debt_to_equity")),
        roe=data.get("roe"),
        roe_formatted=data.get("roe_formatted") or format_percent(data.get("roe")),
        roce=data.get("roce"),
        roce_formatted=data.get("roce_formatted") or format_percent(data.get("roce")),
        analyst_buy_count=data.get("analyst_buy_count"),
        analyst_hold_count=data.get("analyst_hold_count"),
        analyst_sell_count=data.get("analyst_sell_count"),
        analyst_target_mean=data.get("analyst_target_mean"),
        analyst_target_mean_formatted=data.get("analyst_target_mean_formatted") or format_number_amount(data.get("analyst_target_mean")),
        analyst_target_high=data.get("analyst_target_high"),
        analyst_target_high_formatted=data.get("analyst_target_high_formatted") or format_number_amount(data.get("analyst_target_high")),
        analyst_target_low=data.get("analyst_target_low"),
        analyst_target_low_formatted=data.get("analyst_target_low_formatted") or format_number_amount(data.get("analyst_target_low")),
        analyst_recommendation=data.get("analyst_recommendation"),
        promoter_holding_pct=data.get("promoter_holding_pct"),
        promoter_holding_pct_formatted=data.get("promoter_holding_pct_formatted") or format_percent(data.get("promoter_holding_pct")),
        fii_holding_pct=data.get("fii_holding_pct"),
        institutional_holding_pct_formatted=data.get("institutional_holding_pct_formatted") or format_percent(data.get("fii_holding_pct")),
        dii_holding_pct=data.get("dii_holding_pct"),
        public_holding_pct=data.get("public_holding_pct"),
        public_holding_pct_formatted=data.get("public_holding_pct_formatted") or format_percent(data.get("public_holding_pct")),
        quarterly_financials=quarterly,
        outlook_months=settings.outlook_months,
        outlook_high=data.get("outlook_high"),
        outlook_high_formatted=data.get("outlook_high_formatted") or format_number_amount(data.get("outlook_high")),
        outlook_low=data.get("outlook_low"),
        outlook_low_formatted=data.get("outlook_low_formatted") or format_number_amount(data.get("outlook_low")),
        outlook_price_trend=trend,
        custom_metrics=data.get("custom_metrics", {}),
        unavailable_fields=unavailable,
        fetched_at=date.today(),
    )


# ---------------------------------------------------------------------------
# Python Calculation Sandbox (AST Evaluator)
# ---------------------------------------------------------------------------

class _SafeFinancialEvalVisitor(ast.NodeVisitor):
    def __init__(self, context: dict[str, Any]):
        self.context = context

    def visit(self, node):
        method = "visit_" + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node):
        raise ValueError(f"AST node type {node.__class__.__name__} disallowed by security policy")

    def visit_Expression(self, node: ast.Expression):
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value)}")

    def visit_Num(self, node):
        return node.n

    def visit_Str(self, node):
        return node.s

    def visit_Name(self, node: ast.Name):
        if node.id.startswith("_"):
            raise ValueError(f"Access to private identifiers disallowed: {node.id}")
        if node.id not in self.context:
            raise KeyError(f"Missing required field: {node.id}")
        val = self.context[node.id]
        if val is None:
            raise KeyError(f"Missing required field: {node.id}")
        return val

    def visit_List(self, node: ast.List):
        return [self.visit(elt) for elt in node.elts]

    def visit_Tuple(self, node: ast.Tuple):
        return tuple(self.visit(elt) for elt in node.elts)

    def visit_Dict(self, node: ast.Dict):
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def visit_Attribute(self, node: ast.Attribute):
        raise ValueError("AST node type Attribute disallowed by security policy")

    def visit_Subscript(self, node: ast.Subscript):
        val = self.visit(node.value)
        if isinstance(node.slice, ast.Constant):
            idx = node.slice.value
        elif isinstance(node.slice, ast.UnaryOp) and isinstance(node.slice.op, ast.USub) and isinstance(node.slice.operand, ast.Constant):
            idx = -node.slice.operand.value
        elif isinstance(node.slice, ast.Slice):
            lower = self.visit(node.slice.lower) if node.slice.lower else None
            upper = self.visit(node.slice.upper) if node.slice.upper else None
            step = self.visit(node.slice.step) if node.slice.step else None
            return val[slice(lower, upper, step)]
        else:
            idx = self.visit(node.slice)

        if not isinstance(idx, (int, str)):
            raise ValueError(f"Unsupported index type: {type(idx)}")
        return val[idx]

    def visit_UnaryOp(self, node: ast.UnaryOp):
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        elif isinstance(node.op, ast.USub):
            return -operand
        elif isinstance(node.op, ast.Not):
            return not operand
        raise ValueError(f"Unsupported unary operator: {type(node.op)}")

    def visit_BinOp(self, node: ast.BinOp):
        left = self.visit(node.left)
        right = self.visit(node.right)

        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            raise ValueError(f"Arithmetic operators require numeric operands, got {type(left)} and {type(right)}")

        if isinstance(node.op, ast.Add):
            return left + right
        elif isinstance(node.op, ast.Sub):
            return left - right
        elif isinstance(node.op, ast.Mult):
            return left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left / right
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left // right
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise ZeroDivisionError("Modulo by zero")
            return left % right
        elif isinstance(node.op, ast.Pow):
            if left <= 0 and (isinstance(right, float) and not right.is_integer() or right < 0 and left == 0):
                raise ValueError("Cannot compute fractional power / CAGR with non-positive base value")
            try:
                res = left ** right
                if isinstance(res, complex):
                    raise ValueError("Cannot compute fractional power / CAGR with non-positive base value")
                return res
            except Exception as exc:
                raise ValueError(f"Power calculation error: {exc}")
        raise ValueError(f"Unsupported binary operator: {type(node.op)}")

    def visit_Call(self, node: ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("AST node type disallowed by security policy: Only direct named function calls are supported")
        func_name = node.func.id
        if func_name.startswith("_") or func_name not in self.context:
            raise ValueError(f"AST node type disallowed by security policy: Function '{func_name}' is not permitted in calculation sandbox")
        fn = self.context[func_name]
        if not callable(fn):
            raise ValueError(f"'{func_name}' is not callable")

        args = [self.visit(arg) for arg in node.args]
        kwargs = {kw.arg: self.visit(kw.value) for kw in node.keywords}
        return fn(*args, **kwargs)


def _safe_cagr(start_val: float, end_val: float, periods: float) -> float:
    if start_val is None or end_val is None or periods is None:
        raise KeyError("Missing arguments for CAGR calculation")
    if start_val <= 0:
        raise ValueError("Cannot compute CAGR with non-positive base period")
    if periods <= 0:
        raise ZeroDivisionError("Periods must be greater than 0")
    if end_val <= 0:
        raise ValueError("Cannot compute CAGR with non-positive ending period")
    return (end_val / start_val) ** (1.0 / periods) - 1.0


def _safe_fcf_yield(fcf: float, market_cap: float) -> float:
    if fcf is None or market_cap is None:
        raise KeyError("Missing arguments for Free Cash Flow Yield")
    if market_cap <= 0:
        raise ZeroDivisionError("Market cap must be positive")
    return fcf / market_cap


def _safe_margin(numerator: float, denominator: float) -> float:
    if numerator is None or denominator is None:
        raise KeyError("Missing arguments for Margin calculation")
    if denominator == 0:
        raise ZeroDivisionError("Denominator cannot be 0")
    return numerator / denominator


def _safe_spread(val1: float, val2: float) -> float:
    if val1 is None or val2 is None:
        raise KeyError("Missing arguments for Spread calculation")
    return val1 - val2


def compute_custom_financial_metric(
    expression: str,
    context: Optional[dict[str, Any]] = None,
    ticker: Optional[str] = None,
    metric_name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Safely execute an ad-hoc financial expression within a hardened AST sandbox.
    Enforces 2-decimal rounding, percentage casting, zero-division protection,
    non-positive CAGR checks, and quarterly date-continuity validation.
    """
    clean_expr = expression.strip()
    name = metric_name or clean_expr.replace(" ", "_")[:40]

    # Initialize execution namespace
    eval_ctx: dict[str, Any] = {
        "cagr": _safe_cagr,
        "fcf_yield": _safe_fcf_yield,
        "margin": _safe_margin,
        "spread": _safe_spread,
        "min": min,
        "max": max,
        "sum": sum,
        "abs": abs,
        "round": round,
        "pow": pow,
        "len": len,
    }

    chronological_valid = True
    gap_note = None

    # Load ticker context if available
    if ticker:
        try:
            t = yf.Ticker(ticker)
            merged = {}
            merged.update(get_price_snapshot(ticker))
            merged.update(get_valuation_multiples(ticker))
            merged.update(get_fundamentals(ticker))
            merged.update(get_technicals(ticker))
            merged.update(get_ownership(ticker))
            qfins = get_quarterly_financials(ticker)
            merged["quarterly_financials"] = qfins

            # Extract chronological series (oldest first for forward CAGR/trends)
            q_oldest_first = list(reversed(qfins))
            merged["quarterly_revenues"] = [q["revenue"] for q in q_oldest_first if q.get("revenue") is not None]
            merged["quarterly_net_incomes"] = [q["net_income"] for q in q_oldest_first if q.get("net_income") is not None]

            # Check if any quarter has a gap note
            for q in qfins:
                if q.get("data_gap_note"):
                    chronological_valid = False
                    gap_note = q["data_gap_note"]
                    break

            # Try extracting Cash Flow items
            try:
                cf = t.cashflow if t.cashflow is not None and not t.cashflow.empty else t.quarterly_cashflow
                if cf is not None and not cf.empty:
                    cf_idx = {str(i).strip().lower(): i for i in cf.index}
                    for k in ("operating cash flow", "total cash from operating activities", "operatingcashflow"):
                        if k in cf_idx and pd.notna(cf.loc[cf_idx[k]].iloc[0]):
                            merged["operating_cash_flow"] = float(cf.loc[cf_idx[k]].iloc[0])
                            break
                    for k in ("capital expenditure", "capital expenditures", "capex"):
                        if k in cf_idx and pd.notna(cf.loc[cf_idx[k]].iloc[0]):
                            merged["capex"] = abs(float(cf.loc[cf_idx[k]].iloc[0]))
                            break
                    if "operating_cash_flow" in merged and "capex" in merged:
                        merged["free_cash_flow"] = merged["operating_cash_flow"] - merged["capex"]
            except Exception as cf_exc:
                logger.debug("Cash flow extraction in sandbox: %s", cf_exc)

            eval_ctx.update(merged)
        except Exception as ticker_exc:
            logger.warning("Could not auto-populate ticker context for %s: %s", ticker, ticker_exc)

    # Supplement/override with explicitly passed context
    if context:
        eval_ctx.update(context)

    # 1. Parse AST
    try:
        parsed = ast.parse(clean_expr, mode="eval")
    except SyntaxError as syn_err:
        return {
            "status": "error",
            "metric_name": name,
            "value": None,
            "raw_value": None,
            "formatted_value": "data unavailable",
            "formatted": "data unavailable",
            "expression": clean_expr,
            "chronological_valid": False,
            "reason": f"Syntax error in expression: {syn_err}",
        }

    # 2. Evaluate AST with hardened visitor
    try:
        visitor = _SafeFinancialEvalVisitor(eval_ctx)
        result = visitor.visit(parsed)
    except KeyError as k_err:
        reason = str(k_err).strip("'\"")
        if not reason.startswith("Missing required field"):
            reason = f"Missing required field: {reason}"
        return {
            "status": "error",
            "metric_name": name,
            "value": None,
            "raw_value": None,
            "formatted_value": "data unavailable",
            "formatted": "data unavailable",
            "expression": clean_expr,
            "chronological_valid": chronological_valid,
            "reason": reason,
        }
    except ZeroDivisionError as zd_err:
        return {
            "status": "error",
            "metric_name": name,
            "value": None,
            "raw_value": None,
            "formatted_value": "data unavailable",
            "formatted": "data unavailable",
            "expression": clean_expr,
            "chronological_valid": chronological_valid,
            "reason": f"ZeroDivisionError: {zd_err or 'Division by zero'}",
        }
    except ValueError as v_err:
        reason = str(v_err)
        if "non-positive" in reason.lower() or "negative base" in reason.lower():
            return {
                "status": "ok",
                "metric_name": name,
                "value": None,
                "raw_value": None,
                "formatted_value": "N/A (negative base period)",
                "formatted": "N/A (negative base period)",
                "expression": clean_expr,
                "chronological_valid": False,
                "notes": "Base period value is non-positive",
                "reason": reason,
            }
        else:
            return {
                "status": "error",
                "metric_name": name,
                "value": None,
                "raw_value": None,
                "formatted_value": "data unavailable",
                "formatted": "data unavailable",
                "expression": clean_expr,
                "chronological_valid": False,
                "reason": reason,
            }
    except Exception as general_err:
        return {
            "status": "error",
            "metric_name": name,
            "value": None,
            "raw_value": None,
            "formatted_value": "data unavailable",
            "formatted": "data unavailable",
            "expression": clean_expr,
            "chronological_valid": False,
            "reason": f"Execution error: {general_err}",
        }

    # 3. Format and sanitize result
    if result is None:
        return {
            "status": "error",
            "metric_name": name,
            "value": None,
            "raw_value": None,
            "formatted_value": "data unavailable",
            "formatted": "data unavailable",
            "expression": clean_expr,
            "chronological_valid": chronological_valid,
            "reason": "Result evaluated to None",
        }

    if isinstance(result, (int, float)):
        raw_val = round(float(result), 4)

        # Detect percentage / ratio formatting
        is_directional = any(kw in clean_expr.lower() for kw in ("cagr", "growth", "qoq", "yoy", "spread"))
        is_pct = is_directional or any(kw in clean_expr.lower() for kw in ("yield", "margin", "roe", "roce", "pct", "percent"))
        if is_pct or (abs(raw_val) <= 1.0 and any(kw in clean_expr.lower() for kw in ("margin", "cagr", "yield", "spread"))):
            if abs(raw_val) > 1.0:
                metric_val = round(raw_val, 2)
                fmt_val = format_percent(metric_val, include_sign=is_directional)
            else:
                metric_val = round(raw_val * 100.0, 2)
                fmt_val = format_percent(raw_val, include_sign=is_directional)
        else:
            metric_val = round(raw_val, 2)
            fmt_val = format_number_amount(raw_val)

        return {
            "status": "ok",
            "metric_name": name,
            "value": metric_val,
            "raw_value": raw_val,
            "formatted_value": fmt_val,
            "formatted": fmt_val,
            "expression": clean_expr,
            "chronological_valid": chronological_valid,
            "notes": gap_note or f"Computed deterministically via calculation sandbox ({clean_expr})",
        }

    # Non-numeric result (e.g. boolean, string)
    return {
        "status": "ok",
        "metric_name": name,
        "value": result,
        "raw_value": result,
        "formatted_value": str(result),
        "formatted": str(result),
        "expression": clean_expr,
        "chronological_valid": chronological_valid,
        "notes": f"Computed via calculation sandbox ({clean_expr})",
    }


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

