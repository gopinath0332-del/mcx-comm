"""
tests/test_lot_sizer.py
Tests for ATR-based lot sizing formula.
"""
import pytest
from src.lot_sizer import LotSizer


class TestLotSizer:
    def _sizer(self, capital=200000, risk_pct=2.0, lot_mult=1):
        return LotSizer(
            capital=capital,
            base_risk_pct=risk_pct,
            lot_multiplier=lot_mult,
            atr_size_multiplier=1.5,
            max_lots=20,
        )

    def test_basic_calculation(self):
        sizer = self._sizer()
        # risk_amount = 200000 * 2% = 4000
        # stop_dist   = ATR(50) * 1.5 * 1 = 75
        # lots        = floor(4000 / 75) = 53  → capped at 20
        lots = sizer.calculate(atr=50.0)
        assert lots == 20   # max_lots cap

    def test_high_atr_gives_fewer_lots(self):
        sizer = self._sizer()
        # risk=4000, stop=200*1.5=300, lots=floor(4000/300)=13
        lots = sizer.calculate(atr=200.0)
        assert lots == 13

    def test_low_atr_capped_at_max(self):
        sizer = self._sizer(capital=200000)
        # Very low ATR → many lots, should cap at max_lots=20
        lots = sizer.calculate(atr=1.0)
        assert lots == 20

    def test_zero_atr_returns_1(self):
        sizer = self._sizer()
        lots = sizer.calculate(atr=0.0)
        assert lots == 1

    def test_negative_atr_returns_1(self):
        sizer = self._sizer()
        lots = sizer.calculate(atr=-10.0)
        assert lots == 1

    def test_minimum_is_1(self):
        # Very high ATR, tiny capital
        sizer = LotSizer(capital=1000, base_risk_pct=0.1, lot_multiplier=1,
                         atr_size_multiplier=1.5, max_lots=20)
        lots = sizer.calculate(atr=10000.0)
        assert lots == 1

    def test_lot_multiplier_effect(self):
        # Lot mult = 10 (like CRUDEOIL Mini) increases stop distance by 10x
        sizer_1  = LotSizer(capital=200000, base_risk_pct=2.0, lot_multiplier=1,
                            atr_size_multiplier=1.5, max_lots=50)
        sizer_10 = LotSizer(capital=200000, base_risk_pct=2.0, lot_multiplier=10,
                            atr_size_multiplier=1.5, max_lots=50)
        lots_1  = sizer_1.calculate(atr=50.0)
        lots_10 = sizer_10.calculate(atr=50.0)
        # lot_mult=10 means stop_dist is 10x larger → ~10x fewer lots
        assert lots_10 < lots_1

    def test_describe_returns_string(self):
        sizer = self._sizer()
        desc = sizer.describe(atr=100.0)
        assert isinstance(desc, str)
        assert "lot" in desc.lower()
