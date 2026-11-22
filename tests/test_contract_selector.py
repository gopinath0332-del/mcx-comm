"""
tests/test_contract_selector.py
Tests for MCX contract selection logic (no live Kite calls needed).
"""
import pytest
from datetime import date, datetime
from unittest.mock import MagicMock

from src.contract_selector import ContractSelector

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


def _make_instruments(expiries: list[date]) -> list[dict]:
    """Build fake MCX instrument list entries."""
    return [
        {
            "tradingsymbol": f"GOLDPETAL26{d.strftime('%b').upper()}FUT",
            "instrument_token": 1000000 + i,
            "instrument_type": "FUT",
            "name": "GOLDPETAL",
            "expiry": d,
        }
        for i, d in enumerate(expiries)
    ]


class TestContractSelector:
    def test_selects_nearest_next_month_expiry(self, monkeypatch):
        selector = ContractSelector(instrument_prefix="GOLDPETAL", exchange="MCX")
        instruments = _make_instruments([
            date(2026, 11, 28),   # near/current month
            date(2026, 12, 29),   # next month
            date(2027, 1, 30),
        ])
        selector._instruments_cache = instruments
        monkeypatch.setattr(
            "src.contract_selector.datetime",
            type("FakeDT", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 21, 10, 0, tzinfo=IST))}),
        )
        symbol, token = selector._select_next_month(instruments)
        # Should select next month (DEC), not near month (NOV)
        assert symbol == "GOLDPETAL26DECFUT"
        assert token == 1000001

    def test_skips_expired_contracts(self, monkeypatch):
        selector = ContractSelector(instrument_prefix="GOLDPETAL", exchange="MCX")
        instruments = _make_instruments([
            date(2026, 11, 10),   # expired
            date(2026, 12, 29),   # valid
        ])
        selector._instruments_cache = instruments
        monkeypatch.setattr(
            "src.contract_selector.datetime",
            type("FakeDT", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 21, 10, 0, tzinfo=IST))}),
        )
        symbol, token = selector._select_next_month(instruments)
        assert "DEC" in symbol

    def test_raises_when_no_future_contracts(self, monkeypatch):
        selector = ContractSelector(instrument_prefix="GOLDPETAL", exchange="MCX")
        instruments = _make_instruments([
            date(2026, 11, 10),   # all expired
        ])
        monkeypatch.setattr(
            "src.contract_selector.datetime",
            type("FakeDT", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 21, 10, 0, tzinfo=IST))}),
        )
        with pytest.raises(RuntimeError, match="No GOLDPETAL futures"):
            selector._select_next_month(instruments)

    def test_uses_open_position_symbol_directly(self):
        selector = ContractSelector()
        kite_mock = MagicMock()
        kite_mock.instruments.return_value = _make_instruments([
            date(2026, 11, 28),
            date(2026, 12, 29),
        ])
        sym, token = selector.get_active_contract(kite_mock, open_position_symbol="GOLDPETAL26NOVFUT")
        assert sym == "GOLDPETAL26NOVFUT"
        # Token lookup may return 0 if not found in cache but symbol is preserved

    def test_get_contract_expiry(self):
        selector = ContractSelector()
        kite_mock = MagicMock()
        kite_mock.instruments.return_value = [
            {
                "tradingsymbol": "GOLDPETAL26DECFUT",
                "instrument_token": 148129543,
                "instrument_type": "FUT",
                "name": "GOLDPETAL",
                "expiry": "2026-12-31",
            },
            {
                "tradingsymbol": "GOLDPETAL27JANFUT",
                "instrument_token": 148916743,
                "instrument_type": "FUT",
                "name": "GOLDPETAL",
                "expiry": date(2027, 1, 29),
            },
        ]
        dec_exp = selector.get_contract_expiry(kite_mock, "GOLDPETAL26DECFUT")
        assert dec_exp == date(2026, 12, 31)

        jan_exp = selector.get_contract_expiry(kite_mock, "GOLDPETAL27JANFUT")
        assert jan_exp == date(2027, 1, 29)

        missing = selector.get_contract_expiry(kite_mock, "NONEXISTENT")
        assert missing is None

    def test_clear_cache(self):
        selector = ContractSelector()
        selector._instruments_cache = [{"mock": 1}]
        selector.clear_cache()
        assert selector._instruments_cache is None

    def test_tender_cutoff_fallback(self, monkeypatch):
        selector = ContractSelector(instrument_prefix="GOLDPETAL", exchange="MCX")
        # All contracts are within 8 days of today (Nov 21)
        instruments = _make_instruments([
            date(2026, 11, 25),   # 4 days away
            date(2026, 11, 28),   # 7 days away
        ])
        selector._instruments_cache = instruments
        monkeypatch.setattr(
            "src.contract_selector.datetime",
            type("FakeDT", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 21, 10, 0, tzinfo=IST))}),
        )
        # Should fallback to nearest future contract > today rather than throwing
        sym, tok = selector._select_next_month(instruments, tender_cutoff_days=8)
        assert sym == "GOLDPETAL26NOVFUT"
