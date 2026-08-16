import pandas as pd
import pytest

from tools.finance_tools import (
    _compute_macd,
    _compute_roce,
    _compute_rsi,
    _volume_trend,
)


def test_compute_rsi_insufficient_data():
    closes = pd.Series([100.0, 101.0, 102.0])
    assert _compute_rsi(closes, period=14) is None


def test_compute_rsi_all_gains():
    # 20 days of strictly increasing prices -> RSI should be 100
    closes = pd.Series([float(100 + i) for i in range(20)])
    rsi = _compute_rsi(closes, period=14)
    assert rsi is not None
    assert rsi == 100.0


def test_compute_rsi_standard_range():
    # Normal alternating price movement
    prices = [100.0, 102.0, 101.0, 103.0, 102.5, 104.0, 103.0, 105.0,
              104.5, 106.0, 105.0, 107.0, 106.5, 108.0, 107.0, 109.0]
    closes = pd.Series(prices)
    rsi = _compute_rsi(closes, period=14)
    assert rsi is not None
    assert 0.0 <= rsi <= 100.0


def test_compute_macd():
    # Insufficient data
    assert _compute_macd(pd.Series([10.0] * 20)) == (None, None, None)

    # 40 days of price data
    closes = pd.Series([100.0 + (i * 0.5) for i in range(45)])
    macd, signal, hist = _compute_macd(closes)
    assert macd is not None
    assert signal is not None
    assert hist is not None
    # For steadily rising prices, MACD > 0
    assert macd > 0


def test_volume_trend():
    # Insufficient length
    assert _volume_trend(pd.Series([1000] * 30)) is None

    # 60 days total: first 40 days avg 1000, last 20 days avg 2000 -> rising
    vols = pd.Series([1000.0] * 40 + [2000.0] * 20)
    assert _volume_trend(vols) == "rising"

    # 60 days total: first 40 days avg 2000, last 20 days avg 500 -> falling
    vols_falling = pd.Series([2000.0] * 40 + [500.0] * 20)
    assert _volume_trend(vols_falling) == "falling"

    # Flat volume
    vols_flat = pd.Series([1000.0] * 60)
    assert _volume_trend(vols_flat) == "flat"


def test_compute_roce():
    # Normal case: EBIT=200, TotalAssets=1000, CurrentLiabilities=200 -> Capital Employed=800 -> ROCE=0.25
    info = {
        "ebit": 200.0,
        "totalAssets": 1000.0,
        "currentLiabilities": 200.0,
    }
    roce = _compute_roce(info)
    assert roce == 0.25

    # Missing fields
    assert _compute_roce({"ebit": 200.0}) is None
    assert _compute_roce({}) is None


def test_build_quarterly_financials_continuity_gap():
    from unittest.mock import MagicMock
    from tools.finance_tools import _build_quarterly_financials

    # 4 columns newest first: 2025-03-31, 2024-12-31, 2024-09-30, 2024-03-31 (6-month gap)
    dates = [
        pd.Timestamp("2025-03-31"),
        pd.Timestamp("2024-12-31"),
        pd.Timestamp("2024-09-30"),
        pd.Timestamp("2024-03-31"),
    ]
    df = pd.DataFrame(
        {
            dates[0]: [1000.0, 200.0],
            dates[1]: [900.0, 180.0],
            dates[2]: [800.0, 150.0],
            dates[3]: [700.0, 120.0],
        },
        index=["Total Revenue", "Net Income"],
    )
    mock_ticker = MagicMock()
    mock_ticker.quarterly_financials = df

    quarters = _build_quarterly_financials(mock_ticker)
    assert len(quarters) == 4
    # The 2024-09-30 quarter (index 2 in newest-first) comes after the 2024-03-31 quarter in chronological order
    assert quarters[2].data_gap_note == "A prior quarter may be missing from source data (yfinance)."
    assert quarters[0].data_gap_note is None
    assert quarters[1].data_gap_note is None


def test_compute_roce_from_financial_statements():
    from unittest.mock import MagicMock
    from tools.finance_tools import _compute_roce

    mock_ticker = MagicMock()
    mock_ticker.income_stmt = pd.DataFrame(
        {"2025-03-31": [600.0]},
        index=["Operating Income"],
    )
    mock_ticker.quarterly_income_stmt = None
    mock_ticker.balance_sheet = pd.DataFrame(
        {"2025-03-31": [2000.0, 500.0]},
        index=["Total Assets", "Current Liabilities"],
    )
    mock_ticker.quarterly_balance_sheet = None

    # Capital Employed = 2000 - 500 = 1500; ROCE = 600 / 1500 = 0.4
    roce = _compute_roce({}, t=mock_ticker)
    assert roce == 0.4


def test_get_fundamentals_analyst_and_targets():
    from unittest.mock import patch, MagicMock
    from tools.finance_tools import get_fundamentals

    mock_ticker = MagicMock()
    mock_ticker.info = {"trailingEps": 120.0, "recommendationKey": "buy"}
    mock_ticker.recommendations = pd.DataFrame(
        [{"strongBuy": 5, "buy": 15, "hold": 8, "sell": 2, "strongSell": 1}]
    )
    mock_ticker.analyst_price_targets = {
        "mean": 2456.122,
        "high": 3480.0,
        "low": 1800.0,
    }
    mock_ticker.income_stmt = None
    mock_ticker.quarterly_income_stmt = None
    mock_ticker.balance_sheet = None
    mock_ticker.quarterly_balance_sheet = None

    with patch("yfinance.Ticker", return_value=mock_ticker):
        res = get_fundamentals("TEST.NS")
        assert res["analyst_buy_count"] == 20
        assert res["analyst_hold_count"] == 8
        assert res["analyst_sell_count"] == 3
        assert res["analyst_target_mean"] == 2456.12
        assert res["analyst_target_mean_formatted"] == "2,456.12"
        assert res["analyst_target_high_formatted"] == "3,480.00"
        assert res["analyst_target_low_formatted"] == "1,800.00"


def test_custom_financial_metric_cagr_success():
    from tools.finance_tools import compute_custom_financial_metric
    
    # 100 to 200 in 3 years -> (200/100)**(1/3) - 1 = ~25.99%
    res = compute_custom_financial_metric(
        expression="cagr(beginning_val, ending_val, 3)",
        context={"beginning_val": 100.0, "ending_val": 200.0},
        metric_name="3y_revenue_cagr",
    )
    assert res["status"] == "ok"
    assert res["value"] == 25.99
    assert res["formatted_value"] == "+25.99%"
    assert res["metric_name"] == "3y_revenue_cagr"


def test_custom_financial_metric_cagr_negative_base():
    from tools.finance_tools import compute_custom_financial_metric

    # Beginning value is negative
    res = compute_custom_financial_metric(
        expression="cagr(beginning_val, ending_val, 3)",
        context={"beginning_val": -50.0, "ending_val": 100.0},
        metric_name="net_income_cagr",
    )
    assert res["status"] == "ok"
    assert res["value"] is None
    assert res["formatted_value"] == "N/A (negative base period)"
    assert res["notes"] == "Base period value is non-positive"


def test_custom_financial_metric_zero_division():
    from tools.finance_tools import compute_custom_financial_metric

    res = compute_custom_financial_metric(
        expression="operating_cash_flow / market_cap",
        context={"operating_cash_flow": 500.0, "market_cap": 0.0},
        metric_name="fcf_yield",
    )
    assert res["status"] == "error"
    assert res["value"] is None
    assert res["formatted_value"] == "data unavailable"
    assert "ZeroDivisionError" in res["reason"]


def test_custom_financial_metric_missing_variable():
    from tools.finance_tools import compute_custom_financial_metric

    res = compute_custom_financial_metric(
        expression="operating_cash_flow - capex",
        context={"operating_cash_flow": 500.0},
        metric_name="fcf",
    )
    assert res["status"] == "error"
    assert res["value"] is None
    assert res["formatted_value"] == "data unavailable"
    assert "Missing required field" in res["reason"]
    assert "capex" in res["reason"]


def test_custom_financial_metric_ast_security_rejections():
    from tools.finance_tools import compute_custom_financial_metric

    # Reject __import__ or open
    res1 = compute_custom_financial_metric(
        expression="__import__('os').system('dir')",
        context={},
    )
    assert res1["status"] == "error"
    assert "security policy" in res1["reason"]

    # Reject attribute traversal like [].__class__
    res2 = compute_custom_financial_metric(
        expression="a.__class__",
        context={"a": 10},
    )
    assert res2["status"] == "error"
    assert "security policy" in res2["reason"]

    # Reject lambda
    res3 = compute_custom_financial_metric(
        expression="(lambda x: x + 1)(5)",
        context={},
    )
    assert res3["status"] == "error"
    assert "security policy" in res3["reason"]


def test_custom_financial_metric_fcf_yield_and_spread():
    from tools.finance_tools import compute_custom_financial_metric

    res = compute_custom_financial_metric(
        expression="fcf_yield(fcf, market_cap)",
        context={"fcf": 2500000000.0, "market_cap": 50000000000.0},
        metric_name="fcf_yield",
    )
    assert res["status"] == "ok"
    assert res["value"] == 5.0
    assert res["formatted_value"] == "5.00%"

    res_spread = compute_custom_financial_metric(
        expression="spread(return_on_capital, cost_of_capital)",
        context={"return_on_capital": 0.18, "cost_of_capital": 0.11},
        metric_name="economic_spread",
    )
    assert res_spread["status"] == "ok"
    assert res_spread["value"] == 7.0
    assert res_spread["formatted_value"] == "+7.00%"
