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
