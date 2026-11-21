"""
tests/test_candle_builder.py
Tests for CandleBuilder (1H tick accumulation) and HeikinAshiConverter.
"""
import pytest
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from src.candle_builder import CandleBuilder, HeikinAshiConverter, Candle

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


def _ts(h: int, m: int, day: int = 1) -> datetime:
    """Helper to build IST datetime."""
    return datetime(2026, 11, day, h, m, 0, tzinfo=IST)


class TestCandleBuilder:
    def test_first_tick_does_not_close(self):
        cb = CandleBuilder(interval_minutes=60)
        closed = cb.on_tick(100.0, 10, _ts(9, 0))
        assert not closed

    def test_same_hour_ticks_accumulate(self):
        cb = CandleBuilder(interval_minutes=60)
        cb.on_tick(100.0, 10, _ts(9, 0))
        cb.on_tick(105.0, 20, _ts(9, 30))
        cb.on_tick(98.0,  15, _ts(9, 59))
        # Still within the 09:xx hour — no close
        assert cb.last_closed_candle is None

    def test_new_hour_closes_previous_candle(self):
        cb = CandleBuilder(interval_minutes=60)
        cb.on_tick(100.0, 10, _ts(9, 0))
        cb.on_tick(110.0, 20, _ts(9, 30))
        cb.on_tick(95.0,  15, _ts(9, 55))
        # 10:00 tick should close the 09:xx candle
        closed = cb.on_tick(102.0, 5, _ts(10, 0))
        assert closed
        c = cb.last_closed_candle
        assert c is not None
        assert c.open  == 100.0
        assert c.high  == 110.0
        assert c.low   == 95.0
        assert c.close == 95.0    # last price in the 09:xx window
        assert c.timestamp.hour == 9

    def test_zero_ltp_ignored(self):
        cb = CandleBuilder(interval_minutes=60)
        cb.on_tick(100.0, 10, _ts(9, 0))
        closed = cb.on_tick(0.0, 0, _ts(9, 5))
        assert not closed

    def test_volume_accumulates(self):
        cb = CandleBuilder(interval_minutes=60)
        cb.on_tick(100.0, 10, _ts(9, 0))
        cb.on_tick(101.0, 20, _ts(9, 20))
        cb.on_tick(102.0, 30, _ts(10, 0))   # close the first candle
        candle = cb.last_closed_candle
        assert candle.volume == 30   # 10 + 20 from the 09:xx candle


class TestHeikinAshiConverter:
    def _make_df(self, rows):
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"])

    def test_first_bar_ha_close(self):
        df = self._make_df([[100, 110, 90, 105]])
        result = HeikinAshiConverter.convert(df)
        expected_close = (100 + 110 + 90 + 105) / 4
        assert abs(result["ha_close"].iloc[0] - expected_close) < 1e-9

    def test_first_bar_ha_open(self):
        df = self._make_df([[100, 110, 90, 105]])
        result = HeikinAshiConverter.convert(df)
        expected_open = (100 + 105) / 2
        assert abs(result["ha_open"].iloc[0] - expected_open) < 1e-9

    def test_second_bar_ha_open(self):
        df = self._make_df([
            [100, 110, 90, 105],
            [106, 115, 100, 112],
        ])
        result = HeikinAshiConverter.convert(df)
        prev_ha_open  = result["ha_open"].iloc[0]
        prev_ha_close = result["ha_close"].iloc[0]
        expected = (prev_ha_open + prev_ha_close) / 2
        assert abs(result["ha_open"].iloc[1] - expected) < 1e-9

    def test_ha_high_is_max(self):
        df = self._make_df([[100, 90, 80, 85]])   # high=90 < open=(100+85)/2=92.5
        result = HeikinAshiConverter.convert(df)
        ha_open  = result["ha_open"].iloc[0]
        ha_close = result["ha_close"].iloc[0]
        assert result["ha_high"].iloc[0] == max(90, ha_open, ha_close)

    def test_ha_low_is_min(self):
        df = self._make_df([[100, 110, 95, 108]])
        result = HeikinAshiConverter.convert(df)
        ha_open  = result["ha_open"].iloc[0]
        ha_close = result["ha_close"].iloc[0]
        assert result["ha_low"].iloc[0] == min(95, ha_open, ha_close)

    def test_empty_df_returns_empty(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close"])
        result = HeikinAshiConverter.convert(df)
        assert result.empty
