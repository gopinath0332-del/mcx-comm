"""
tests/test_session_manager.py
==============================
Unit tests for SessionManager REST-based polling, market hour checks,
candle time verification, and historical pre-seeding.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import MagicMock, patch

import pytest

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

from config.settings import Settings
from src.session_manager import (
    SessionManager,
    _is_market_hours,
    _is_weekend,
)


@pytest.fixture
def dummy_settings():
    """Return Settings instance with mock parameters."""
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
            settings = Settings()
            return settings


@pytest.fixture
def mock_kite():
    kite = MagicMock()
    kite.margins.return_value = {
        "available": {"live_balance": 200000.0, "cash": 200000.0}
    }
    kite.ltp.return_value = {
        "MCX:GOLDPETAL26DECFUT": {"last_price": 8550.0}
    }
    return kite


@pytest.fixture
def mock_notifier():
    return MagicMock()


# ----------------------------------------------------------------------
# 1. Market Hours and Weekend Helpers
# ----------------------------------------------------------------------

def test_is_weekend():
    # 2026-11-21 is Saturday, 2026-11-22 is Sunday
    sat = datetime(2026, 11, 21, 10, 0, tzinfo=IST)
    sun = datetime(2026, 11, 22, 10, 0, tzinfo=IST)
    mon = datetime(2026, 11, 23, 10, 0, tzinfo=IST)
    assert _is_weekend(sat) is True
    assert _is_weekend(sun) is True
    assert _is_weekend(mon) is False


def test_is_market_hours():
    open_t = (9, 0)
    close_t = (23, 30)

    # Monday 08:59 (closed)
    dt1 = datetime(2026, 11, 23, 8, 59, tzinfo=IST)
    assert _is_market_hours(dt1, open_t, close_t) is False

    # Monday 09:00 (open)
    dt2 = datetime(2026, 11, 23, 9, 0, tzinfo=IST)
    assert _is_market_hours(dt2, open_t, close_t) is True

    # Monday 15:30 (open)
    dt3 = datetime(2026, 11, 23, 15, 30, tzinfo=IST)
    assert _is_market_hours(dt3, open_t, close_t) is True

    # Monday 23:30 (open)
    dt4 = datetime(2026, 11, 23, 23, 30, tzinfo=IST)
    assert _is_market_hours(dt4, open_t, close_t) is True

    # Monday 23:31 (closed)
    dt5 = datetime(2026, 11, 23, 23, 31, tzinfo=IST)
    assert _is_market_hours(dt5, open_t, close_t) is False

    # Saturday 12:00 (closed)
    dt6 = datetime(2026, 11, 21, 12, 0, tzinfo=IST)
    assert _is_market_hours(dt6, open_t, close_t) is False


# ----------------------------------------------------------------------
# 2. Candle Timing Checks (_is_time_for_candle_check)
# ----------------------------------------------------------------------

def test_is_time_for_candle_check(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26DECFUT"
    sm._active_token = 12345

    # 1. Before 10:00:00 (first candle 09:00-10:00 not closed yet)
    now_0930 = datetime(2026, 11, 23, 9, 30, 0, tzinfo=IST)
    assert sm._is_time_for_candle_check(now_0930) is False

    # 2. At 10:00:06 (first candle is closed + 5s grace period)
    now_1000 = datetime(2026, 11, 23, 10, 0, 6, tzinfo=IST)
    assert sm._is_time_for_candle_check(now_1000) is True

    # 3. If last processed candle was 09:00 (closed at 10:00),
    # next candle closes at 11:00:00. At 10:15:00 it should return False.
    sm._last_processed_candle_time = datetime(2026, 11, 23, 9, 0, 0, tzinfo=IST)
    now_1015 = datetime(2026, 11, 23, 10, 15, 0, tzinfo=IST)
    assert sm._is_time_for_candle_check(now_1015) is False

    # 4. At 11:00:06, next candle (10:00-11:00) is closed
    now_1100 = datetime(2026, 11, 23, 11, 0, 6, tzinfo=IST)
    assert sm._is_time_for_candle_check(now_1100) is True


# ----------------------------------------------------------------------
# 3. Preseed and New Candle Processing
# ----------------------------------------------------------------------

def test_preseed_historical_candles_filters_in_progress(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26DECFUT"
    sm._active_token = 12345

    # Mock historical_data returning 3 candles:
    # 08:00 (closed), 09:00 (closed), and 10:00 (in-progress at 10:30)
    ref_time = datetime(2026, 11, 23, 10, 30, 0, tzinfo=IST)
    candles = [
        {"date": datetime(2026, 11, 23, 8, 0, tzinfo=IST), "open": 8500, "high": 8520, "low": 8490, "close": 8510, "volume": 100},
        {"date": datetime(2026, 11, 23, 9, 0, tzinfo=IST), "open": 8510, "high": 8530, "low": 8505, "close": 8525, "volume": 120},
        {"date": datetime(2026, 11, 23, 10, 0, tzinfo=IST), "open": 8525, "high": 8540, "low": 8520, "close": 8535, "volume": 50},
    ]
    mock_kite.historical_data.return_value = candles

    with patch("src.session_manager.datetime") as mock_dt:
        mock_dt.now.return_value = ref_time
        mock_dt.combine = datetime.combine
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        loaded = sm._preseed_historical_candles()
        # Only 08:00 and 09:00 are closed at 10:30 (10:00 closes at 11:00)
        assert loaded == 2
        assert len(sm._candle_buffer) == 2
        assert sm._last_processed_candle_time == datetime(2026, 11, 23, 9, 0, tzinfo=IST)
        assert sm._last_ltp == 8525.0


def test_check_and_process_new_candles(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26DECFUT"
    sm._active_token = 12345

    # Suppose 09:00 candle was already processed
    sm._last_processed_candle_time = datetime(2026, 11, 23, 9, 0, tzinfo=IST)

    # Now at 11:00:10, 10:00 candle is closed, and 11:00 candle is in progress
    now_time = datetime(2026, 11, 23, 11, 0, 10, tzinfo=IST)
    candles = [
        {"date": datetime(2026, 11, 23, 9, 0, tzinfo=IST), "open": 8500, "high": 8520, "low": 8490, "close": 8510, "volume": 100},
        {"date": datetime(2026, 11, 23, 10, 0, tzinfo=IST), "open": 8510, "high": 8540, "low": 8500, "close": 8530, "volume": 150},
        {"date": datetime(2026, 11, 23, 11, 0, tzinfo=IST), "open": 8530, "high": 8535, "low": 8528, "close": 8532, "volume": 20},
    ]
    mock_kite.historical_data.return_value = candles

    with patch.object(sm, "_process_closed_candle") as mock_process:
        with patch("src.session_manager.datetime") as mock_dt:
            mock_dt.now.return_value = now_time
            mock_dt.combine = datetime.combine

            processed = sm._check_and_process_new_candles()
            assert processed == 1
            assert sm._last_processed_candle_time == datetime(2026, 11, 23, 10, 0, tzinfo=IST)
            assert sm._last_ltp == 8530.0
            mock_process.assert_called_once()
            cand_arg = mock_process.call_args[0][0]
            assert cand_arg["open"] == 8510.0
            assert cand_arg["close"] == 8530.0


def test_fetch_current_ltp(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26DECFUT"
    mock_kite.ltp.return_value = {
        "MCX:GOLDPETAL26DECFUT": {"last_price": 8560.5}
    }
    ltp = sm._fetch_current_ltp()
    assert ltp == 8560.5
    assert sm._last_ltp == 8560.5


def test_expiry_tender_guard_triggers_exit_and_rollover_on_open_position(
    dummy_settings, mock_kite, mock_notifier
):
    from datetime import date
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26NOVFUT"
    sm._active_expiry = date(2026, 11, 28)

    # Simulate open position in NOV contract
    sm._order_mgr.restore_state(
        symbol="GOLDPETAL26NOVFUT", qty=1, position="LONG", entry_price=8500.0
    )
    sm._signal_engine.restore_state("LONG")

    # Today is Nov 22 -> 6 days to expiry (<= cutoff 8 days)
    now_ist = datetime(2026, 11, 22, 15, 0, tzinfo=IST)

    with patch.object(sm._contract_selector, "get_contract_expiry", return_value=date(2026, 11, 28)), \
         patch.object(sm, "_fetch_current_ltp", return_value=8550.0), \
         patch.object(sm, "_rollover_active_contract") as mock_rollover, \
         patch.object(sm._order_mgr, "handle_exit") as mock_exit:

        triggered = sm._check_expiry_tender_guard(now_ist)
        assert triggered is True
        mock_exit.assert_called_once()
        exit_arg = mock_exit.call_args[0][0]
        assert "Tender Expiry Guard" in exit_arg.exit_reason
        mock_rollover.assert_called_once()


def test_expiry_tender_guard_triggers_rollover_without_position(
    dummy_settings, mock_kite, mock_notifier
):
    from datetime import date
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26NOVFUT"
    sm._active_expiry = date(2026, 11, 28)
    sm._order_mgr._symbol = None  # No open position

    # Today is Nov 22 -> 6 days to expiry (<= cutoff 8 days)
    now_ist = datetime(2026, 11, 22, 15, 0, tzinfo=IST)

    with patch.object(sm, "_rollover_active_contract") as mock_rollover:
        triggered = sm._check_expiry_tender_guard(now_ist)
        assert triggered is True
        mock_rollover.assert_called_once()


def test_expiry_tender_guard_no_trigger_outside_cutoff(
    dummy_settings, mock_kite, mock_notifier
):
    from datetime import date
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26DECFUT"
    sm._active_expiry = date(2026, 12, 31)
    sm._order_mgr.restore_state(
        symbol="GOLDPETAL26DECFUT", qty=1, position="LONG", entry_price=8500.0
    )

    # Today is Nov 22 -> 39 days to expiry (> cutoff 8 days)
    now_ist = datetime(2026, 11, 22, 15, 0, tzinfo=IST)

    with patch.object(sm._contract_selector, "get_contract_expiry", return_value=date(2026, 12, 31)), \
         patch.object(sm, "_rollover_active_contract") as mock_rollover:

        triggered = sm._check_expiry_tender_guard(now_ist)
        assert triggered is False
        mock_rollover.assert_not_called()


# ----------------------------------------------------------------------
# 4. Token Expiry & Startup Retry Tests
# ----------------------------------------------------------------------

def test_ensure_authenticated_kite_success(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._running = True
    mock_kite.profile.return_value = {"user_id": "TQL221", "user_name": "Rajaramanathan"}

    sm._ensure_authenticated_kite(retry_delay=1)
    mock_notifier.warning.assert_not_called()


def test_ensure_authenticated_kite_retries_on_token_exception(
    dummy_settings, mock_kite, mock_notifier
):
    from kiteconnect.exceptions import TokenException

    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._running = True

    # 1st call fails with TokenException, 2nd call succeeds
    mock_kite.profile.side_effect = [
        TokenException("Token expired"),
        {"user_id": "TQL221", "user_name": "Rajaramanathan"},
    ]

    with patch("time.sleep") as mock_sleep, \
         patch.object(sm, "_reload_access_token", side_effect=["expired_token", "new_refreshed_token"]):

        sm._ensure_authenticated_kite(retry_delay=1)

        # Discord alert must have been sent
        mock_notifier.warning.assert_called_once_with(
            "⚠️ Zerodha token expired. Waiting for morning refresh..."
        )
        # Sleep was called during retry
        assert mock_sleep.called
        # Token was updated on the kite client
        mock_kite.set_access_token.assert_called_with("new_refreshed_token")
        # Success notification was delivered
        assert mock_notifier.info.called


def test_ensure_authenticated_kite_aborts_on_stop(
    dummy_settings, mock_kite, mock_notifier
):
    from kiteconnect.exceptions import TokenException

    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._running = True

    def stop_during_sleep(*args, **kwargs):
        sm._running = False

    mock_kite.profile.side_effect = TokenException("Token expired")

    with patch("time.sleep", side_effect=stop_during_sleep):
        sm._ensure_authenticated_kite(retry_delay=60)
        assert sm._running is False
        mock_notifier.warning.assert_called_once_with(
            "⚠️ Zerodha token expired. Waiting for morning refresh..."
        )


# ----------------------------------------------------------------------
# 5. Daily Heartbeat / Health Ping & Uptime Tests
# ----------------------------------------------------------------------

def test_get_uptime_str(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    now = datetime.now(tz=IST)

    # 45 minutes
    sm._start_time = now - timedelta(minutes=45)
    assert sm._get_uptime_str() == "45m"

    # 2 hours 15 minutes
    sm._start_time = now - timedelta(hours=2, minutes=15)
    assert sm._get_uptime_str() == "2h 15m"

    # 3 days 5 hours 10 minutes
    sm._start_time = now - timedelta(days=3, hours=5, minutes=10)
    assert sm._get_uptime_str() == "3d 5h 10m"


def test_send_heartbeat(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26DECFUT"
    sm._last_ltp = 8550.0
    sm._candles_today = 5

    now_ist = datetime(2026, 11, 23, 14, 0, tzinfo=IST)
    sm._send_heartbeat(now_ist)

    mock_notifier.heartbeat.assert_called_once()
    kwargs = mock_notifier.heartbeat.call_args[1]
    assert kwargs["contract"] == "GOLDPETAL26DECFUT"
    assert kwargs["candles_evaluated"] == 5
    assert kwargs["position"] == "FLAT"
    assert "Host:" in kwargs["system_info"]
    assert sm._heartbeat_date == now_ist.date()

    # Second call on same date should not re-send
    mock_notifier.heartbeat.reset_mock()
    sm._send_heartbeat(now_ist)
    mock_notifier.heartbeat.assert_not_called()


def test_morning_briefing_includes_uptime(dummy_settings, mock_kite, mock_notifier):
    sm = SessionManager(settings=dummy_settings, kite_client=mock_kite, notifier=mock_notifier)
    sm._active_symbol = "GOLDPETAL26DECFUT"

    sm._send_morning_briefing()
    mock_notifier.morning_briefing.assert_called_once()
    kwargs = mock_notifier.morning_briefing.call_args[1]
    assert "uptime" in kwargs
    assert "system_info" in kwargs
    assert "Host:" in kwargs["system_info"]


def test_discord_notifier_heartbeat_embed():
    from src.discord_notifier import DiscordNotifier
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/test", live_order_placement=False)

    with patch.object(notifier, "_send_embed") as mock_send_embed:
        notifier.heartbeat(
            contract="GOLDPETAL26DECFUT",
            ltp=8550.0,
            position="LONG",
            uptime="1d 4h 10m",
            candles_evaluated=6,
            system_info="Host: raspberrypi • PID: 12345",
        )
        mock_send_embed.assert_called_once()
        title = mock_send_embed.call_args[1]["title"]
        fields = mock_send_embed.call_args[1]["fields"]
        assert "Daily Heartbeat" in title
        field_dict = {f[0]: f[1] for f in fields}
        assert field_dict["Contract"] == "GOLDPETAL26DECFUT"
        assert "8,550.00" in field_dict["LTP"]
        assert "LONG" in field_dict["Position"]
        assert field_dict["Engine Uptime"] == "1d 4h 10m"
        assert "raspberrypi" in field_dict["System Info"]
        assert "🟢 Healthy (Raspberry Pi)" in field_dict["Daemon Status"]
