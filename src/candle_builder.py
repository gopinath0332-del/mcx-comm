"""
src/candle_builder.py
=====================
Accumulates live Kite ticks into 1-hour OHLCV candles, then converts the
rolling buffer into Heikin-Ashi candles for strategy evaluation.

Tick → 1H OHLCV
  CandleBuilder.on_tick(ltp, vol, ts) → returns True when a 1H candle closes

OHLCV DataFrame → Heikin-Ashi DataFrame
  HeikinAshiConverter.convert(df)

Heikin-Ashi formulas (match PineScript):
  HA_Close = (O + H + L + C) / 4
  HA_Open  = (prev_HA_Open + prev_HA_Close) / 2
  HA_High  = max(H, HA_Open, HA_Close)
  HA_Low   = min(L, HA_Open, HA_Close)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


@dataclass
class Candle:
    """A single closed OHLCV candle."""
    timestamp: datetime     # candle open time (IST)
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleBuilder:
    """
    Builds 1-hour OHLCV candles from individual price ticks.

    Each tick updates the in-progress candle.  When the tick's timestamp
    crosses into a new hour boundary, the previous candle is closed and
    made available via `last_closed_candle`.

    Parameters
    ----------
    interval_minutes : int
        Candle width in minutes.  Should be 60 for the MCX gold strategy.
    """

    def __init__(self, interval_minutes: int = 60) -> None:
        self._interval = timedelta(minutes=interval_minutes)
        self._current_open_time: datetime | None = None
        self._open: float = 0.0
        self._high: float = 0.0
        self._low: float = float("inf")
        self._close: float = 0.0
        self._volume: float = 0.0
        self.last_closed_candle: Candle | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_tick(self, ltp: float, vol: float, ts: datetime) -> bool:
        """
        Process one price tick.

        Returns True when a new candle has just closed (and is available
        in ``self.last_closed_candle``).  Returns False otherwise.

        Parameters
        ----------
        ltp : float  Last traded price.
        vol : float  Volume traded at this tick (cumulative or incremental).
        ts  : datetime  Tick timestamp (any tz; will be converted to IST).
        """
        if ltp <= 0:
            return False

        # Normalise to IST
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ist = ts.astimezone(IST)

        # Compute candle-open time (floor to interval boundary)
        candle_open = self._floor_to_interval(ts_ist)

        # First tick ever
        if self._current_open_time is None:
            self._start_new_candle(candle_open, ltp, vol)
            return False

        # Still within the same candle period
        if candle_open == self._current_open_time:
            self._update_candle(ltp, vol)
            return False

        # New period — close the current candle
        self.last_closed_candle = Candle(
            timestamp=self._current_open_time,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
        logger.debug(
            "Candle closed: %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            self._current_open_time.strftime("%Y-%m-%d %H:%M"),
            self._open, self._high, self._low, self._close, self._volume,
        )

        # Start the new candle
        self._start_new_candle(candle_open, ltp, vol)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _floor_to_interval(self, ts: datetime) -> datetime:
        """Return ts floored down to the nearest interval boundary."""
        total_minutes = ts.hour * 60 + ts.minute
        interval_minutes = int(self._interval.total_seconds() // 60)
        floored_minutes = (total_minutes // interval_minutes) * interval_minutes
        return ts.replace(
            hour=floored_minutes // 60,
            minute=floored_minutes % 60,
            second=0,
            microsecond=0,
        )

    def _start_new_candle(self, open_time: datetime, ltp: float, vol: float) -> None:
        self._current_open_time = open_time
        self._open  = ltp
        self._high  = ltp
        self._low   = ltp
        self._close = ltp
        self._volume = vol

    def _update_candle(self, ltp: float, vol: float) -> None:
        if ltp > self._high:
            self._high = ltp
        if ltp < self._low:
            self._low = ltp
        self._close = ltp
        self._volume += vol


# ======================================================================
# Heikin-Ashi Converter
# ======================================================================

class HeikinAshiConverter:
    """
    Converts a standard OHLCV DataFrame into Heikin-Ashi candles.

    The DataFrame must have columns: open, high, low, close (and optionally volume).
    The result DataFrame has columns: ha_open, ha_high, ha_low, ha_close
    plus the original columns retained as-is.
    """

    @staticmethod
    def convert(df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute Heikin-Ashi OHLC and append as ha_open/ha_high/ha_low/ha_close.

        Parameters
        ----------
        df : pd.DataFrame
            Must contain columns: open, high, low, close.

        Returns
        -------
        pd.DataFrame
            Original df with extra columns: ha_open, ha_high, ha_low, ha_close.
        """
        if df.empty:
            return df

        df = df.copy()
        n = len(df)

        ha_close = np.zeros(n)
        ha_open  = np.zeros(n)
        ha_high  = np.zeros(n)
        ha_low   = np.zeros(n)

        opens  = df["open"].to_numpy(dtype=float)
        highs  = df["high"].to_numpy(dtype=float)
        lows   = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)

        # First bar: HA_Open = (Open + Close) / 2
        ha_close[0] = (opens[0] + highs[0] + lows[0] + closes[0]) / 4.0
        ha_open[0]  = (opens[0] + closes[0]) / 2.0
        ha_high[0]  = max(highs[0], ha_open[0], ha_close[0])
        ha_low[0]   = min(lows[0],  ha_open[0], ha_close[0])

        for i in range(1, n):
            ha_close[i] = (opens[i] + highs[i] + lows[i] + closes[i]) / 4.0
            ha_open[i]  = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
            ha_high[i]  = max(highs[i], ha_open[i], ha_close[i])
            ha_low[i]   = min(lows[i],  ha_open[i], ha_close[i])

        df["ha_close"] = ha_close
        df["ha_open"]  = ha_open
        df["ha_high"]  = ha_high
        df["ha_low"]   = ha_low

        return df
