"""
tests/test_signal_engine.py
Tests for the Gold Trend Filter signal evaluation logic.
"""
import pytest
from datetime import datetime, time
from types import SimpleNamespace

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

from src.signal_engine import SignalEngine


def _settings(**overrides):
    """Build a minimal settings-like namespace."""
    defaults = dict(
        allow_long=True,
        allow_short=True,
        atr_trail_multiplier=2.0,
        min_long_bars=0,
        skip_opening_candles=True,
        bars_to_skip=2,
        entry_cutoff_hhmm=(22, 0),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _row(ha_close, upper, lower, ema_trend, atr):
    """Build a signal-row dict."""
    return {
        "ha_close":  ha_close,
        "ha_high":   ha_close + 5,
        "ha_low":    ha_close - 5,
        "upper":     upper,
        "lower":     lower,
        "ema_trend": ema_trend,
        "atr":       atr,
    }


def _ts(hour=15, minute=0, day=3):
    """Return an IST datetime — day=3 is a weekday."""
    return datetime(2026, 11, day, hour, minute, 0, tzinfo=IST)


class TestLongEntry:
    def test_long_entry_fires_above_upper_and_ema(self):
        se = SignalEngine(_settings())
        # Bars 1 and 2 are blocked by skip-opening-candle filter.
        # Use close < upper so they don't trigger entry accidentally.
        se.evaluate(_row(80, 95, 70, 90, 5), _ts(9))   # bar 1 — blocked
        se.evaluate(_row(80, 95, 70, 90, 5), _ts(10))  # bar 2 — blocked
        # Bar 3 onwards allowed: close >= upper AND close > ema → BUY
        result = se.evaluate(_row(101, 100, 70, 90, 5), _ts(11))
        assert result.action == "BUY"

    def test_no_long_below_ema(self):
        se = SignalEngine(_settings())
        for i in range(3):
            se.evaluate(_row(100, 95, 85, 90, 5), _ts(9 + i))
        # close > upper but close < ema_trend → no long
        result = se.evaluate(_row(101, 100, 85, 110, 5), _ts(12))
        assert result.action == "NONE"

    def test_no_entry_in_first_2_bars(self):
        se = SignalEngine(_settings())
        # Bar 1 — should be blocked (day_bar_count=1 <= bars_to_skip=2)
        result = se.evaluate(_row(101, 100, 85, 90, 5), _ts(9))
        assert result.action == "NONE"
        # Bar 2 — blocked
        result = se.evaluate(_row(101, 100, 85, 90, 5), _ts(10))
        assert result.action == "NONE"
        # Bar 3 — allowed
        result = se.evaluate(_row(101, 100, 85, 90, 5), _ts(11))
        assert result.action == "BUY"

    def test_no_entry_after_cutoff(self):
        se = SignalEngine(_settings(entry_cutoff_hhmm=(22, 0)))
        # First 3 bars
        for i in range(3):
            se.evaluate(_row(100, 95, 85, 90, 5), _ts(9 + i))
        # 23:00 is past cutoff
        result = se.evaluate(_row(101, 100, 85, 90, 5), _ts(23))
        assert result.action == "NONE"

    def test_no_double_long_while_in_position(self):
        se = SignalEngine(_settings())
        for i in range(3):
            se.evaluate(_row(100, 95, 85, 90, 5), _ts(9 + i))
        se.evaluate(_row(101, 100, 85, 90, 5), _ts(12))  # opens LONG
        # Another breakout bar while long — should not fire again
        result = se.evaluate(_row(102, 101, 85, 90, 5), _ts(13))
        assert result.action == "NONE"


class TestShortEntry:
    def test_short_entry_below_lower_and_ema(self):
        se = SignalEngine(_settings())
        for i in range(3):
            se.evaluate(_row(100, 110, 100, 90, 5), _ts(9 + i))
        # close <= lower AND close < ema_trend
        result = se.evaluate(_row(89, 110, 90, 95, 5), _ts(12))
        assert result.action == "SELL"

    def test_no_short_above_ema(self):
        se = SignalEngine(_settings())
        for i in range(3):
            se.evaluate(_row(100, 110, 100, 90, 5), _ts(9 + i))
        # close <= lower but close > ema_trend
        result = se.evaluate(_row(89, 110, 90, 80, 5), _ts(12))
        assert result.action == "NONE"

    def test_no_short_when_long_only_mode(self):
        se = SignalEngine(_settings(allow_short=False))
        for i in range(3):
            se.evaluate(_row(100, 110, 100, 90, 5), _ts(9 + i))
        result = se.evaluate(_row(89, 110, 90, 95, 5), _ts(12))
        assert result.action == "NONE"


class TestLongExit:
    def _open_long(self, se):
        """Helper to open a long position."""
        for i in range(3):
            se.evaluate(_row(100, 95, 85, 90, 5), _ts(9 + i))
        se.evaluate(_row(101, 100, 85, 90, 5), _ts(12))
        assert se.position == "LONG"

    def test_channel_stop_exits_long(self):
        se = SignalEngine(_settings())
        self._open_long(se)
        # close <= lower → EXIT_BUY
        result = se.evaluate(_row(84, 100, 85, 90, 5), _ts(13))
        assert result.action == "EXIT_BUY"
        assert "Channel Stop" in result.exit_reason

    def test_trailing_sl_exits_long(self):
        se = SignalEngine(_settings())
        self._open_long(se)
        # First update: trail = close - atr*2 = 101 - 5*2 = 91; trail should be 91
        # Drop below trail
        result = se.evaluate(_row(89, 100, 86, 90, 5), _ts(13))
        assert result.action == "EXIT_BUY"
        assert "Trailing SL" in result.exit_reason


class TestShortExit:
    def _open_short(self, se):
        for i in range(3):
            se.evaluate(_row(100, 110, 100, 90, 5), _ts(9 + i))
        se.evaluate(_row(89, 110, 90, 95, 5), _ts(12))
        assert se.position == "SHORT"

    def test_channel_stop_exits_short(self):
        se = SignalEngine(_settings())
        self._open_short(se)
        # close >= upper → EXIT_SELL
        result = se.evaluate(_row(111, 110, 90, 95, 5), _ts(13))
        assert result.action == "EXIT_SELL"

    def test_trailing_sl_exits_short(self):
        se = SignalEngine(_settings())
        self._open_short(se)
        # trail for short = close + atr*2 = 89 + 5*2 = 99 on entry bar
        # close >= trail on next bar
        result = se.evaluate(_row(100, 110, 90, 95, 5), _ts(13))
        assert result.action == "EXIT_SELL"


class TestForceExit:
    def test_force_exit_while_long(self):
        se = SignalEngine(_settings())
        for i in range(3):
            se.evaluate(_row(100, 95, 85, 90, 5), _ts(9 + i))
        se.evaluate(_row(101, 100, 85, 90, 5), _ts(12))
        result = se.force_exit("EOD Watchdog", _ts(23, 25), atr=5.0)
        assert result.action == "EXIT_BUY"
        assert se.position is None

    def test_force_exit_when_flat_is_noop(self):
        se = SignalEngine(_settings())
        result = se.force_exit("EOD Watchdog", _ts(23, 25), atr=5.0)
        assert result.action == "NONE"


class TestStateRestore:
    def test_restore_long_position(self):
        se = SignalEngine(_settings())
        se.restore_state(position="LONG", long_trail=95.0, short_trail=None)
        assert se.position == "LONG"
        assert se.long_trail == 95.0
