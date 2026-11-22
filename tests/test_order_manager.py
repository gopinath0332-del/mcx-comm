"""
tests/test_order_manager.py
===========================
Unit tests for OrderManager including margin pre-flight check and order placement.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

from config.settings import Settings
from src.lot_sizer import LotSizer
from src.order_manager import OrderManager
from src.signal_engine import SignalResult
from src.state_manager import StateManager


@pytest.fixture
def mock_settings():
    with patch("config.settings._load_yaml_settings") as mock_yaml:
        mock_yaml.return_value = {
            "strategy_name": "GoldTrendFilter",
            "instrument_prefix": "MCX:GOLDPETAL",
            "capital": 200000.0,
            "base_risk_pct": 2.0,
            "lot_multiplier": 1,
            "trade_mode": "Both",
            "max_lots": None,
            "enter_channel": 20,
            "exit_channel": 10,
            "ema_length": 200,
            "atr_period": 20,
            "atr_tp_multiplier": 4.0,
            "atr_trail_multiplier": 2.0,
            "enable_partial_tp": False,
            "min_long_days": 0,
            "skip_opening_enabled": True,
            "bars_to_skip": 2,
            "candle_interval_minutes": 60,
            "market_open": "09:00",
            "market_close": "23:30",
            "entry_cutoff": "22:00",
            "eod_exit": "23:25",
            "enforce_market_hours": True,
            "warmup_candles": 50,
            "use_atr_sizing": True,
            "log_level": "INFO",
        }
        with patch.dict(
            "os.environ",
            {
                "KITE_API_KEY": "test_api_key",
                "KITE_ACCESS_TOKEN": "test_access_token",
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
                "LIVE_ORDER_PLACEMENT": "false",
            },
        ):
            return Settings()


@pytest.fixture
def order_manager(mock_settings, tmp_path):
    kite = MagicMock()
    notifier = MagicMock()
    state_mgr = StateManager(state_path=str(tmp_path / "state.json"))
    lot_sizer = LotSizer(
        capital=mock_settings.capital,
        base_risk_pct=mock_settings.base_risk_pct,
        lot_multiplier=mock_settings.lot_multiplier,
    )
    return OrderManager(
        kite=kite,
        notifier=notifier,
        state_manager=state_mgr,
        settings=mock_settings,
        lot_sizer=lot_sizer,
    )


def test_get_available_margin_commodity_success(order_manager):
    order_manager._kite.margins.return_value = {
        "available": {"live_balance": 75000.0, "cash": 75000.0}
    }
    bal = order_manager._get_available_margin()
    assert bal == 75000.0


def test_get_available_margin_equity_fallback(order_manager):
    # Commodity balance is 0, equity has balance
    order_manager._kite.margins.side_effect = [
        {"available": {"live_balance": 0.0, "cash": 0.0}},  # segment="commodity"
        {"equity": {"available": {"live_balance": 150000.0, "cash": 150000.0}}},  # all segments
    ]
    bal = order_manager._get_available_margin()
    assert bal == 150000.0


def test_estimate_margin_per_lot_from_api(order_manager):
    order_manager._kite.order_margins.return_value = [{"total": 1450.0}]
    margin = order_manager._estimate_margin_per_lot("GOLDPETAL26DECFUT", "BUY", 12400.0)
    assert margin == 1450.0


def test_estimate_margin_per_lot_fallback(order_manager):
    order_manager._kite.order_margins.side_effect = Exception("API error")
    margin = order_manager._estimate_margin_per_lot("GOLDPETAL26DECFUT", "BUY", 12500.0)
    # 12% of 12,500 = 1,500
    assert margin == 1500.0


def test_margin_preflight_caps_risk_lots(order_manager):
    # Risk lots = 50, but available cash = 30,000, margin per lot = 1500
    # Affordable lots = 20 -> should cap to 20
    order_manager._kite.margins.return_value = {
        "available": {"live_balance": 30000.0, "cash": 30000.0}
    }
    order_manager._kite.order_margins.return_value = [{"total": 1500.0}]

    final_lots, desc = order_manager._apply_margin_preflight(
        symbol="GOLDPETAL26DECFUT",
        transaction_type="BUY",
        risk_lots=50,
        entry_price=12400.0,
        atr=50.0,
    )
    assert final_lots == 20
    assert "Margin Capped" in desc


def test_margin_preflight_sufficient_margin(order_manager):
    # Risk lots = 20, available cash = 100,000, margin per lot = 1500
    # Affordable lots = 66 -> stays at 20
    order_manager._kite.margins.return_value = {
        "available": {"live_balance": 100000.0, "cash": 100000.0}
    }
    order_manager._kite.order_margins.return_value = [{"total": 1500.0}]

    final_lots, desc = order_manager._apply_margin_preflight(
        symbol="GOLDPETAL26DECFUT",
        transaction_type="BUY",
        risk_lots=20,
        entry_price=12400.0,
        atr=50.0,
    )
    assert final_lots == 20
    assert "Margin Verified" in desc


def test_margin_preflight_aborts_on_zero_margin(order_manager):
    order_manager._s.live_order_placement = True  # In live mode
    order_manager._kite.margins.return_value = {
        "available": {"live_balance": 0.0, "cash": 0.0}
    }
    order_manager._kite.order_margins.return_value = [{"total": 1500.0}]

    final_lots, desc = order_manager._apply_margin_preflight(
        symbol="GOLDPETAL26DECFUT",
        transaction_type="BUY",
        risk_lots=20,
        entry_price=12400.0,
        atr=50.0,
    )
    assert final_lots == 0
    assert "Aborted" in desc
    order_manager._notifier.error.assert_called_once()


def test_handle_buy_respects_margin_cap(order_manager):
    order_manager._kite.margins.return_value = {
        "available": {"live_balance": 30000.0, "cash": 30000.0}
    }
    order_manager._kite.order_margins.return_value = [{"total": 1500.0}]

    now_ist = datetime(2026, 11, 23, 11, 0, tzinfo=IST)
    sig = SignalResult(
        action="BUY",
        entry_price=12500.0,
        trail_sl=12300.0,
        atr=50.0,  # risk_lots = 53
        candle_time=now_ist,
    )

    order_manager.handle_buy("GOLDPETAL26DECFUT", 12345, sig, now_ist)
    assert order_manager.open_position == "LONG"
    # Should be capped at 20 lots instead of 53
    assert order_manager.open_qty == 20
