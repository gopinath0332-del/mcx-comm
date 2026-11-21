"""
src/indicators.py
=================
Computes all technical indicators required by the Gold Trend Filter strategy.

Indicators (applied to Heikin-Ashi OHLC columns):
  - Donchian Channel (enter_period=20, exit_period=10)   → upper, lower
  - EMA trend filter (ema_length=200)                    → ema_trend
  - ATR — EMA of True Range (atr_period=20)              → atr

All calculations match PineScript v4 semantics:
  - Donchian uses shift(1): today's signal uses yesterday's highest/lowest
  - ATR uses ema() (exponential, not RMA) to match the pine script
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ======================================================================
# Main entry point
# ======================================================================

def apply_all(df: pd.DataFrame, settings) -> pd.DataFrame:
    """
    Apply all indicators to the DataFrame (which must have ha_open/ha_high/
    ha_low/ha_close columns from HeikinAshiConverter).

    Returns the same DataFrame with indicator columns added.
    """
    df = compute_donchian(df, settings.enter_channel, settings.exit_channel)
    df = compute_ema(df, settings.ema_length)
    df = compute_atr(df, settings.atr_period)
    return df


# ======================================================================
# Donchian Channel
# ======================================================================

def compute_donchian(
    df: pd.DataFrame,
    enter_period: int = 20,
    exit_period: int = 10,
) -> pd.DataFrame:
    """
    Compute Donchian channels on Heikin-Ashi OHLC.

    PineScript equivalents:
      upper = highest(enter_period)   → shift(1) applied for signal
      lower = lowest(exit_period)     → shift(1) applied for signal

    Columns added:
      upper : highest HA_High over previous `enter_period` bars (shift 1)
      lower : lowest  HA_Low  over previous `exit_period`  bars (shift 1)

    The shift(1) mirrors PineScript's `highest(enter_period)` which uses
    the previous bar's highest, so a breakout signal fires on bar close.
    """
    # Rolling max/min on the HA highs/lows, then shift by 1 so the signal
    # references the PREVIOUS bar's channel (matching pine `upper[1]`)
    df["upper"] = df["ha_high"].rolling(window=enter_period).max().shift(1)
    df["lower"] = df["ha_low"].rolling(window=exit_period).min().shift(1)
    return df


# ======================================================================
# EMA Trend Filter
# ======================================================================

def compute_ema(df: pd.DataFrame, length: int = 200) -> pd.DataFrame:
    """
    Compute EMA of HA close for trend filter.
    PineScript: emaTrend = ema(close, emaLength)
    """
    df["ema_trend"] = df["ha_close"].ewm(span=length, adjust=False).mean()
    return df


# ======================================================================
# ATR (EMA of True Range — matches PineScript ema(tr(true), period))
# ======================================================================

def compute_atr(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """
    Compute ATR as EMA of True Range.

    PineScript: atr = ema(tr(true), atrperiod)

    tr(true) uses previous close in the True Range:
      TR = max(high - low, abs(high - prev_close), abs(low - prev_close))

    Note: uses ha_high/ha_low but regular close for prev_close term
    (HA candles already smooth, but TR still uses raw prev_close convention).
    We use ha_high/ha_low for the HA-based range.
    """
    ha_high  = df["ha_high"]
    ha_low   = df["ha_low"]
    ha_close = df["ha_close"]
    prev_close = ha_close.shift(1)

    tr = pd.concat([
        ha_high - ha_low,
        (ha_high - prev_close).abs(),
        (ha_low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # EMA of TR (matches PineScript ema())
    df["atr"] = tr.ewm(span=period, adjust=False).mean()
    return df
