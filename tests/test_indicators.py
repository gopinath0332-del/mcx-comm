"""
tests/test_indicators.py
Tests for Donchian channel, EMA and ATR calculations.
"""
import pytest
import pandas as pd
import numpy as np

from src.indicators import compute_donchian, compute_ema, compute_atr


def _ha_df(n=30, base=100.0, step=1.0):
    """Generate a simple trending HA-like DataFrame for testing."""
    closes = [base + i * step for i in range(n)]
    highs  = [c + 5  for c in closes]
    lows   = [c - 5  for c in closes]
    opens  = [c - 2  for c in closes]
    return pd.DataFrame({
        "ha_open":  opens,
        "ha_high":  highs,
        "ha_low":   lows,
        "ha_close": closes,
        "open":     opens,
        "high":     highs,
        "low":      lows,
        "close":    closes,
    })


class TestDonchian:
    def test_upper_is_shifted_max_high(self):
        df = _ha_df(n=30)
        result = compute_donchian(df, enter_period=20, exit_period=10)
        # Row index 25: upper should be max(ha_high rows 5..24) shifted back
        # i.e., the highest of the previous 20 bars' ha_high
        row25_upper = result["upper"].iloc[25]
        expected = df["ha_high"].iloc[5:25].max()
        assert abs(row25_upper - expected) < 1e-9

    def test_lower_is_shifted_min_low(self):
        df = _ha_df(n=25)
        result = compute_donchian(df, enter_period=20, exit_period=10)
        row20_lower = result["lower"].iloc[20]
        expected = df["ha_low"].iloc[10:20].min()
        assert abs(row20_lower - expected) < 1e-9

    def test_first_rows_are_nan(self):
        df = _ha_df(n=25)
        result = compute_donchian(df, enter_period=20, exit_period=10)
        # First enter_period rows should be NaN (not enough data + shift)
        assert pd.isna(result["upper"].iloc[0])

    def test_channels_are_correct_direction(self):
        df = _ha_df(n=30)
        result = compute_donchian(df, enter_period=20, exit_period=10)
        valid = result.dropna()
        assert (valid["upper"] >= valid["lower"]).all()


class TestEMA:
    def test_ema_length_200_does_not_crash(self):
        df = _ha_df(n=250)
        result = compute_ema(df, length=200)
        assert "ema_trend" in result.columns
        assert not result["ema_trend"].isna().all()

    def test_ema_converges_on_flat_series(self):
        # Constant series: EMA should converge to that constant
        df = _ha_df(n=50, step=0.0)
        result = compute_ema(df, length=10)
        # After warm-up, EMA should equal the constant close value
        assert abs(result["ema_trend"].iloc[-1] - 100.0) < 1e-6


class TestATR:
    def test_atr_is_positive(self):
        df = _ha_df(n=30)
        result = compute_atr(df, period=20)
        valid = result["atr"].dropna()
        assert (valid > 0).all()

    def test_atr_flat_market(self):
        # If high-low = 10 always and no gaps, ATR ≈ 10
        n = 50
        df = pd.DataFrame({
            "ha_open":  [100.0] * n,
            "ha_high":  [105.0] * n,
            "ha_low":   [95.0]  * n,
            "ha_close": [100.0] * n,
            "open":     [100.0] * n,
            "high":     [105.0] * n,
            "low":      [95.0]  * n,
            "close":    [100.0] * n,
        })
        result = compute_atr(df, period=14)
        # After warm-up, ATR should be close to 10
        assert abs(result["atr"].iloc[-1] - 10.0) < 1.0
