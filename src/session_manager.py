"""
src/session_manager.py
=======================
Top-level orchestrator for the MCX Gold commodity trading engine.
Operates via lightweight, scheduled REST API polling (no WebSocket needed).

Data flow (Hourly REST Poll):
  Hourly Timer (HH:00:05 IST)
    └─► Kite REST: historical_data(interval="60minute")
          └─► [1H candle closed]
                └─► HeikinAshiConverter.convert(buffer)
                      └─► apply_all(indicators)
                            └─► SignalEngine.evaluate()
                                  └─► OrderManager.handle_*(symbol, qty)
                                        └─► DiscordNotifier.*()

MCX Gold trading hours: 09:00–23:30 IST, Monday–Friday
Candle interval: 60 minutes (1H Heikin-Ashi)
"""

from __future__ import annotations

import logging
import time as _time
from collections import deque
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

from config.settings import Settings
from src.candle_builder import Candle, HeikinAshiConverter
from src.contract_selector import ContractSelector
from src.discord_notifier import DiscordNotifier
from src.indicators import apply_all
from src.lot_sizer import LotSizer
from src.order_manager import OrderManager
from src.signal_engine import SignalEngine, SignalResult
from src.state_manager import StateManager

logger = logging.getLogger(__name__)

# Minimum candles in buffer before signals are evaluated
# (needs at least enter_channel=20 candles for valid indicators)
MIN_CANDLES = 25

# Polling parameters
POLL_INTERVAL_SECONDS = 10
CANDLE_CLOSE_GRACE_SECONDS = 5


def _is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5


def _is_market_hours(dt: datetime, open_hhmm: tuple, close_hhmm: tuple) -> bool:
    """Returns True if dt is within MCX active trading hours."""
    if _is_weekend(dt):
        return False
    t = dt.time()
    return time(*open_hhmm) <= t <= time(*close_hhmm)


class SessionManager:
    """
    Manages a full MCX Gold intraday trading session via REST polling.

    Parameters
    ----------
    settings : Settings
        Loaded application settings.
    kite_client : KiteConnect | None
        Injected authenticated Kite client (useful for testing).
    notifier : DiscordNotifier | None
        Injected notifier instance.
    """

    def __init__(
        self,
        settings: Settings,
        kite_client=None,
        notifier: DiscordNotifier | None = None,
    ) -> None:
        self._s = settings

        # Build Kite REST client
        from src.kite_auth import build_authenticated_kite
        self._kite = kite_client or build_authenticated_kite(
            settings.kite_api_key, settings.kite_access_token
        )

        # Core components
        self._notifier = notifier or DiscordNotifier(
            webhook_url=settings.discord_webhook_url,
            live_order_placement=settings.live_order_placement,
        )
        self._state_mgr     = StateManager()
        self._signal_engine = SignalEngine(settings)
        self._lot_sizer     = LotSizer(
            capital=settings.capital,
            base_risk_pct=settings.base_risk_pct,
            lot_multiplier=settings.lot_multiplier,
            max_lots=getattr(settings, "max_lots", None),
        )
        self._order_mgr = OrderManager(
            kite=self._kite,
            notifier=self._notifier,
            state_manager=self._state_mgr,
            settings=settings,
            lot_sizer=self._lot_sizer,
        )

        # Contract selection
        self._contract_selector = ContractSelector(
            instrument_prefix=settings.instrument_prefix,
            exchange="MCX",
        )
        self._active_symbol: str = ""
        self._active_token: int  = 0
        self._active_expiry: date | None = None

        # Candle buffer & state tracking
        self._candle_buffer: deque[dict] = deque(maxlen=500)
        self._running: bool              = False
        self._last_ltp: float            = 0.0
        self._last_processed_candle_time: datetime | None = None

        # Session stats
        self._candles_today: int         = 0
        self._session_date               = datetime.now(tz=IST).date()
        self._morning_briefing_date      = None
        self._start_time: datetime       = datetime.now(tz=IST)
        self._heartbeat_date: date | None = None

    # ------------------------------------------------------------------
    # Startup & Main Execution Loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main entry point. Enforces MCX market hours via REST polling."""
        mode = "LIVE" if self._s.live_order_placement else "PAPER"
        logger.info("MCX Gold Engine starting in %s mode (REST Polling).", mode)
        self._running = True

        # 0. Ensure authenticated Kite connection (handles token expiry on startup gracefully)
        self._ensure_authenticated_kite()
        if not self._running:
            return

        # 1. Select contract (respects open position rule & tender guard)
        open_sym = self._state_mgr.load()
        open_sym_str = open_sym.symbol if open_sym else None
        self._active_symbol, self._active_token = (
            self._contract_selector.get_active_contract(
                self._kite, open_sym_str, tender_cutoff_days=self._s.tender_cutoff_days
            )
        )
        self._active_expiry = self._contract_selector.get_contract_expiry(
            self._kite, self._active_symbol
        )
        logger.info(
            "Active contract: %s (token=%d, expiry=%s)",
            self._active_symbol, self._active_token,
            self._active_expiry.strftime("%Y-%m-%d") if self._active_expiry else "N/A",
        )

        # 2. Reconcile state with broker
        self._state_mgr.reconcile(
            kite=self._kite,
            signal_engine=self._signal_engine,
            order_manager=self._order_mgr,
            live_mode=self._s.live_order_placement,
        )

        # 3. Pre-seed historical candles (warm up indicators)
        self._preseed_historical_candles()

        # 4. Main session lifecycle loop
        while self._running:
            now_ist = datetime.now(tz=IST)

            if not self._s.enforce_market_hours:
                logger.info("Market hours enforcement disabled. Running active session.")
                self._run_active_session()
                break

            if _is_weekend(now_ist):
                day_name = now_ist.strftime("%A")
                logger.info("⏸️ %s (Weekend) — MCX closed. Entering standby.", day_name)
                self._notifier.info(
                    f"⏸️ **MCX Gold Engine — Weekend Standby**\n"
                    f"• Today is **{day_name}** — MCX is closed.\n"
                    f"• Engine is sleeping, will auto-resume on Monday 09:00 IST."
                )
                self._standby_sleep()
                continue

            if not _is_market_hours(now_ist, self._s.market_open_hhmm, self._s.market_close_hhmm):
                logger.info(
                    "⏸️ Outside MCX hours (%s IST). Entering standby.",
                    now_ist.strftime("%H:%M"),
                )
                self._standby_sleep()
                continue

            # Within active market hours
            self._run_active_session()

    def stop(self) -> None:
        """Gracefully stop session manager and exit."""
        logger.info("Stopping MCX session manager…")
        self._running = False
        self._notifier.info("⏹️ MCX Gold Engine stopped.")
        self._notifier.stop()

    # ------------------------------------------------------------------
    # Startup Token Validation & Morning Refresh Guard
    # ------------------------------------------------------------------

    def _ensure_authenticated_kite(self, retry_delay: int = 60) -> None:
        """
        Validate Kite Connect authentication on startup.
        If token validation fails (e.g. expired overnight before morning auth runs),
        sends a Discord alert:
            "⚠️ Zerodha token expired. Waiting for morning refresh..."
        and sleeps/retries every 60 seconds until the token is updated in .env.
        """
        from kiteconnect.exceptions import TokenException

        alert_sent = False

        while self._running:
            try:
                # 1. Reload the latest token from disk (.env / NSE_ENV_PATH)
                token = self._reload_access_token()
                if not token:
                    raise TokenException("Kite access token is empty or missing from .env")

                # Apply token to kite client
                if hasattr(self._kite, "set_access_token"):
                    self._kite.set_access_token(token)

                # 2. Validate token against Zerodha API
                profile = self._kite.profile()
                user_name = profile.get("user_name", "Trader") if hasattr(profile, "get") else "Trader"
                user_id = profile.get("user_id", "") if hasattr(profile, "get") else ""
                logger.info(
                    "Zerodha Kite authenticated successfully for %s (%s).",
                    user_name, user_id,
                )
                if alert_sent:
                    self._notifier.info(
                        f"✅ **Zerodha Token Refreshed**: Authenticated as {user_name} ({user_id}). Resuming engine startup."
                    )
                return

            except TokenException as exc:
                logger.warning(
                    "Zerodha token validation failed (TokenException: %s).", exc
                )
            except Exception as exc:
                err_msg = str(exc).lower()
                if "token" in err_msg or "session" in err_msg or "403" in err_msg or "forbidden" in err_msg:
                    logger.warning("Zerodha token validation failed: %s.", exc)
                else:
                    logger.warning("Error checking Zerodha token on startup: %s.", exc)

            if not alert_sent:
                logger.warning("Sending Discord notification: token expired, waiting for morning refresh...")
                self._notifier.warning("⚠️ Zerodha token expired. Waiting for morning refresh...")
                alert_sent = True

            logger.info("Waiting %ds for morning token refresh in .env...", retry_delay)
            for _ in range(retry_delay):
                if not self._running:
                    return
                _time.sleep(1)

    def _reload_access_token(self) -> str:
        """Reload latest Kite access token from disk (.env / NSE_ENV_PATH)."""
        if hasattr(self._s, "reload_credentials"):
            return self._s.reload_credentials()
        return self._s.kite_access_token

    # ------------------------------------------------------------------
    # Active Session (REST Polling)
    # ------------------------------------------------------------------

    def _run_active_session(self) -> None:
        """Run REST polling loop during active MCX market hours."""
        if len(self._candle_buffer) < MIN_CANDLES:
            self._preseed_historical_candles()

        self._send_morning_briefing()

        eod_h, eod_m = self._s.eod_exit_hhmm
        close_h, close_m = self._s.market_close_hhmm
        heartbeat_h, heartbeat_m = self._s.heartbeat_hhmm
        eod_target = time(eod_h, eod_m)
        close_target = time(close_h, close_m)
        heartbeat_target = time(heartbeat_h, heartbeat_m)

        logger.info(
            "MCX active polling loop started: EOD exit at %s IST | Market close at %s IST | Heartbeat at %s IST.",
            self._s.eod_exit, self._s.market_close, self._s.heartbeat_time,
        )

        while self._running:
            try:
                now_ist = datetime.now(tz=IST)
                now_t = now_ist.time()

                # 1. Tender Expiry Guard Check
                if self._check_expiry_tender_guard(now_ist):
                    continue

                # 2. EOD force-exit
                if now_t >= eod_target and self._order_mgr.open_symbol is not None:
                    logger.warning(
                        "🚨 [EOD WATCHDOG] Time %s ≥ %s with open position %s. Force exiting.",
                        now_ist.strftime("%H:%M:%S"), self._s.eod_exit,
                        self._order_mgr.open_symbol,
                    )
                    exit_price = self._fetch_current_ltp()
                    result = SignalResult(
                        action="EXIT_BUY" if self._signal_engine.position == "LONG" else "EXIT_SELL",
                        exit_reason=f"EOD Force Exit {self._s.eod_exit}",
                        candle_time=now_ist,
                        atr=0.0,
                    )
                    self._order_mgr.handle_exit(result, now_ist, exit_price=exit_price)
                    self._signal_engine.force_exit(
                        reason="EOD Watchdog", candle_time=now_ist, atr=0.0
                    )

                # 3. Daily Heartbeat / Health Ping
                if self._s.heartbeat_enabled and now_t >= heartbeat_target:
                    self._send_heartbeat(now_ist)

                # 4. Market close check
                if self._s.enforce_market_hours and now_t >= close_target:
                    logger.info("MCX Market close (%s IST). Sending EOD summary.", self._s.market_close)
                    self._send_eod_summary(now_ist)
                    break

                # 5. New Candle evaluation (REST poll)
                if self._is_time_for_candle_check(now_ist):
                    self._check_and_process_new_candles()

            except Exception as exc:
                logger.error("Error in active session polling loop: %s", exc, exc_info=True)

            # Sleep in 1-second chunks up to POLL_INTERVAL_SECONDS for responsive shutdown
            for _ in range(POLL_INTERVAL_SECONDS):
                if not self._running:
                    break
                _time.sleep(1)

    def _is_time_for_candle_check(self, now_ist: datetime) -> bool:
        """
        Check if a newly closed 1H candle could potentially be available.
        Avoids redundant REST calls during intra-hour periods.
        """
        open_h, open_m = self._s.market_open_hhmm
        first_candle_close = datetime.combine(
            now_ist.date(), time(open_h, open_m), tzinfo=IST
        ) + timedelta(minutes=self._s.candle_interval_minutes)

        # MCX first candle (e.g. 09:00–10:00) cannot close before 10:00:00
        if self._s.enforce_market_hours and now_ist < first_candle_close:
            return False

        if self._last_processed_candle_time is None:
            return True

        # Next candle close is 2 intervals from the previous candle's open time
        interval_td = timedelta(minutes=self._s.candle_interval_minutes)
        grace_td = timedelta(seconds=CANDLE_CLOSE_GRACE_SECONDS)

        if self._last_processed_candle_time.date() < now_ist.date():
            # First candle of a new session
            return now_ist >= (first_candle_close + grace_td)

        # Same session: check if current time is past the close of the next candle
        next_expected_close = self._last_processed_candle_time + (2 * interval_td)
        return now_ist >= (next_expected_close + grace_td)

    def _check_and_process_new_candles(self) -> int:
        """
        Fetch recent 1H candles via Kite REST API, and process any newly closed candles.
        Returns the number of new candles processed.
        """
        if not self._kite or not self._active_token:
            return 0

        now_ist = datetime.now(tz=IST)
        from_dt = now_ist.date() - timedelta(days=3)

        try:
            records = self._kite.historical_data(
                instrument_token=self._active_token,
                from_date=from_dt,
                to_date=now_ist,
                interval="60minute",
            )
        except Exception as exc:
            logger.warning("Could not fetch historical candles via REST: %s", exc)
            return 0

        if not records:
            return 0

        interval_td = timedelta(minutes=self._s.candle_interval_minutes)
        new_candles_processed = 0

        for rec in records:
            rec_dt = rec.get("date")
            if hasattr(rec_dt, "tzinfo") and rec_dt.tzinfo:
                ist_ts = rec_dt.astimezone(IST)
            elif hasattr(rec_dt, "replace"):
                ist_ts = rec_dt.replace(tzinfo=IST)
            else:
                ist_ts = now_ist

            # A 60m candle starting at ist_ts is only closed when now_ist >= ist_ts + interval
            if now_ist < (ist_ts + interval_td):
                # In-progress candle, skip
                continue

            # Skip already processed candles
            if self._last_processed_candle_time is not None and ist_ts <= self._last_processed_candle_time:
                continue

            candle_data = {
                "open":      float(rec.get("open",   0.0)),
                "high":      float(rec.get("high",   0.0)),
                "low":       float(rec.get("low",    0.0)),
                "close":     float(rec.get("close",  0.0)),
                "volume":    float(rec.get("volume", 0.0)),
                "timestamp": ist_ts,
                "date":      ist_ts.date(),
            }

            logger.info(
                "🔔 New closed 1H candle received via REST: %s | O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
                ist_ts.strftime("%Y-%m-%d %H:%M"),
                candle_data["open"], candle_data["high"], candle_data["low"],
                candle_data["close"], candle_data["volume"],
            )

            self._last_ltp = candle_data["close"]
            self._last_processed_candle_time = ist_ts
            self._process_closed_candle(candle_data)
            new_candles_processed += 1

        return new_candles_processed

    # ------------------------------------------------------------------
    # Candle Processing Pipeline
    # ------------------------------------------------------------------

    def _process_closed_candle(self, candle: dict | Candle) -> None:
        """
        Called each time a 1H candle closes.
        Runs HA conversion, indicators, and signal evaluation.
        """
        try:
            if isinstance(candle, dict):
                c_open = candle["open"]
                c_high = candle["high"]
                c_low = candle["low"]
                c_close = candle["close"]
                c_vol = candle["volume"]
                c_ts = candle["timestamp"]
            else:
                c_open = candle.open
                c_high = candle.high
                c_low = candle.low
                c_close = candle.close
                c_vol = candle.volume
                c_ts = candle.timestamp

            ist_ts = c_ts.astimezone(IST) if hasattr(c_ts, "tzinfo") and c_ts.tzinfo else c_ts
            c_date = ist_ts.date()

            # Track daily stats
            if c_date != self._session_date:
                self._candles_today = 0
                self._session_date = c_date
            self._candles_today += 1

            # Add to rolling buffer
            self._candle_buffer.append({
                "open":      c_open,
                "high":      c_high,
                "low":       c_low,
                "close":     c_close,
                "volume":    c_vol,
                "timestamp": ist_ts,
                "date":      c_date,
            })

            if len(self._candle_buffer) < MIN_CANDLES:
                logger.debug(
                    "Buffer warming up: %d / %d candles",
                    len(self._candle_buffer), MIN_CANDLES,
                )
                return

            # Build DataFrame and compute indicators on Heikin-Ashi candles
            df = pd.DataFrame(list(self._candle_buffer))
            df = HeikinAshiConverter.convert(df)
            df = apply_all(df, self._s)

            last = df.iloc[-1]

            # Safety: skip if any key indicator is NaN
            if any(
                pd.isna(last.get(col))
                for col in ("ha_close", "upper", "lower", "ema_trend", "atr")
            ):
                logger.debug("Skipping candle %s — NaN in indicators.", ist_ts.strftime("%H:%M"))
                return

            logger.info(
                "📊 Candle %s | HA_C=₹%.2f (Raw C=₹%.2f) | Upper=₹%.2f | Lower=₹%.2f | EMA(200)=₹%.2f | ATR=₹%.2f | Pos=%s",
                ist_ts.strftime("%Y-%m-%d %H:%M"),
                last["ha_close"], last["close"], last["upper"], last["lower"],
                last["ema_trend"], last["atr"],
                self._signal_engine.position or "FLAT",
            )

            # Evaluate signals
            result = self._signal_engine.evaluate(last, ist_ts)

            if result.action == "BUY":
                self._order_mgr.handle_buy(
                    self._active_symbol, self._active_token, result, ist_ts
                )
            elif result.action == "SELL":
                self._order_mgr.handle_sell(
                    self._active_symbol, self._active_token, result, ist_ts
                )
            elif result.action in ("EXIT_BUY", "EXIT_SELL"):
                self._order_mgr.handle_exit(result, ist_ts, exit_price=last["ha_close"])

        except Exception as exc:
            logger.error("Error processing candle at %s: %s", candle, exc, exc_info=True)
            self._notifier.error(f"Candle processing error: {exc}")

    # ------------------------------------------------------------------
    # Live Price Helper
    # ------------------------------------------------------------------

    def _fetch_current_ltp(self) -> float:
        """Fetch live LTP from Kite REST API for the active contract."""
        if not self._kite or not self._active_symbol:
            return self._last_ltp
        try:
            tradingsymbol = f"MCX:{self._active_symbol}"
            res = self._kite.ltp([tradingsymbol])
            if tradingsymbol in res:
                price = float(res[tradingsymbol].get("last_price", 0.0))
                if price > 0:
                    self._last_ltp = price
                    return price
        except Exception as exc:
            logger.debug("Failed to fetch live LTP via REST: %s", exc)
        return self._last_ltp

    # ------------------------------------------------------------------
    # Historical pre-seed
    # ------------------------------------------------------------------

    def _preseed_historical_candles(self) -> int:
        """
        Pre-load recent closed 1H OHLCV candles from Kite historical API into buffer.
        Returns the number of candles loaded.
        """
        if not self._kite or not self._active_token:
            return 0
        try:
            now_ist  = datetime.now(tz=IST)
            from_dt  = now_ist.date() - timedelta(days=15)
            records  = self._kite.historical_data(
                instrument_token=self._active_token,
                from_date=from_dt,
                to_date=now_ist,
                interval="60minute",
            )
            if not records:
                logger.warning("No historical candles returned for %s.", self._active_symbol)
                return 0

            interval_td = timedelta(minutes=self._s.candle_interval_minutes)
            self._candle_buffer.clear()

            # Filter to keep only fully closed candles
            closed_records = []
            for rec in records:
                rec_dt = rec.get("date")
                if hasattr(rec_dt, "tzinfo") and rec_dt.tzinfo:
                    ist_ts = rec_dt.astimezone(IST)
                elif hasattr(rec_dt, "replace"):
                    ist_ts = rec_dt.replace(tzinfo=IST)
                else:
                    ist_ts = now_ist

                if now_ist >= (ist_ts + interval_td):
                    closed_records.append((rec, ist_ts))

            count = self._s.warmup_candles
            recent = closed_records[-count:] if len(closed_records) > count else closed_records

            for rec, ist_ts in recent:
                self._candle_buffer.append({
                    "open":      float(rec.get("open",   0.0)),
                    "high":      float(rec.get("high",   0.0)),
                    "low":       float(rec.get("low",    0.0)),
                    "close":     float(rec.get("close",  0.0)),
                    "volume":    float(rec.get("volume", 0.0)),
                    "timestamp": ist_ts,
                    "date":      ist_ts.date(),
                })
                self._last_processed_candle_time = ist_ts
                self._last_ltp = float(rec.get("close", 0.0))

            logger.info(
                "Pre-seeded %d historical 60minute candles for %s (last closed: %s).",
                len(self._candle_buffer), self._active_symbol,
                self._last_processed_candle_time.strftime("%Y-%m-%d %H:%M") if self._last_processed_candle_time else "None",
            )
            return len(self._candle_buffer)
        except Exception as exc:
            logger.warning("Could not pre-seed historical candles: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Tender / Expiry Guard & Contract Rollover
    # ------------------------------------------------------------------

    def _check_expiry_tender_guard(self, now_ist: datetime) -> bool:
        """
        Check if the open position or active tracking contract has entered the MCX
        tender cutoff window (typically 5 trading days tender period + 3 days buffer).
        If an open position is within the cutoff, force-exit it immediately and rollover.
        If no position is open but active contract is within the cutoff, rollover the contract.
        Returns True if a rollover action was performed, False otherwise.
        """
        if not self._s.expiry_guard_enabled:
            return False

        today = now_ist.date()
        cutoff_days = self._s.tender_cutoff_days

        # Case 1: Open position within tender period
        open_sym = self._order_mgr.open_symbol
        if open_sym is not None:
            expiry = self._contract_selector.get_contract_expiry(self._kite, open_sym)
            if expiry:
                days_left = (expiry - today).days
                if days_left <= cutoff_days:
                    logger.warning(
                        "🚨 [EXPIRY TENDER GUARD] Open position %s expires on %s (%d days <= %d days cutoff). "
                        "Triggering force-exit to prevent physical delivery / penalty margin.",
                        open_sym, expiry, days_left, cutoff_days,
                    )
                    exit_price = self._fetch_current_ltp()
                    result = SignalResult(
                        action="EXIT_BUY" if self._signal_engine.position == "LONG" else "EXIT_SELL",
                        exit_reason=f"Tender Expiry Guard ({days_left}d to expiry ≤ {cutoff_days}d)",
                        candle_time=now_ist,
                        atr=0.0,
                    )
                    self._order_mgr.handle_exit(result, now_ist, exit_price=exit_price)
                    self._signal_engine.force_exit(
                        reason=f"Tender Expiry Guard ({days_left}d to expiry)",
                        candle_time=now_ist,
                        atr=0.0,
                    )
                    self._notifier.warning(
                        f"🚨 **MCX Tender Guard Triggered**: Force exited `{open_sym}` at ₹{exit_price:,.2f} "
                        f"({days_left} days left to expiry, threshold: {cutoff_days} days). Rolling over contract."
                    )
                    self._rollover_active_contract()
                    return True

        # Case 2: Active tracking contract within tender period (no open position)
        if self._active_expiry is not None:
            days_left = (self._active_expiry - today).days
            if days_left <= cutoff_days:
                logger.warning(
                    "⚠️ [EXPIRY TENDER GUARD] Active contract %s expires on %s (%d days <= %d days cutoff). "
                    "Rolling over active tracking contract to next safe month.",
                    self._active_symbol, self._active_expiry, days_left, cutoff_days,
                )
                self._notifier.info(
                    f"🔄 **MCX Tender Guard**: Active contract `{self._active_symbol}` is within "
                    f"tender window ({days_left}d to expiry ≤ {cutoff_days}d). Rolling over to next safe month."
                )
                self._rollover_active_contract()
                return True

        return False

    def _rollover_active_contract(self) -> None:
        """
        Rollover the active contract to the next safe month outside the tender period.
        Clears contract cache, selects new contract, updates token/expiry, and pre-seeds candles.
        """
        logger.info("Initiating contract rollover...")
        old_symbol = self._active_symbol
        self._contract_selector.clear_cache()
        new_symbol, new_token = self._contract_selector.get_active_contract(
            self._kite,
            open_position_symbol=None,
            tender_cutoff_days=self._s.tender_cutoff_days,
        )
        new_expiry = self._contract_selector.get_contract_expiry(self._kite, new_symbol)

        self._active_symbol = new_symbol
        self._active_token = new_token
        self._active_expiry = new_expiry

        logger.info(
            "Rollover complete: %s -> %s (token=%d, expiry=%s)",
            old_symbol, new_symbol, new_token,
            new_expiry.strftime("%Y-%m-%d") if new_expiry else "N/A",
        )
        self._notifier.info(
            f"🔄 **Contract Rollover Complete**: Now tracking `{new_symbol}` "
            f"(Expiry: {new_expiry.strftime('%Y-%m-%d') if new_expiry else 'N/A'})."
        )
        # Clear candle buffer and re-seed historical 1H candles for new contract
        self._candle_buffer.clear()
        self._last_processed_candle_time = None
        self._preseed_historical_candles()

    # ------------------------------------------------------------------
    # Notifications & Standby
    # ------------------------------------------------------------------

    def _get_uptime_str(self) -> str:
        """Return human-readable duration since engine started, e.g. '2d 5h 14m'."""
        now = datetime.now(tz=IST)
        total_seconds = max(0, int((now - self._start_time).total_seconds()))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0 or days > 0:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    def _get_system_info(self) -> str:
        """Return host and OS process ID for daemon monitoring."""
        import os
        import platform
        hostname = platform.node() or "RaspberryPi"
        pid = os.getpid()
        return f"Host: {hostname} • PID: {pid}"

    def _send_morning_briefing(self) -> None:
        now_ist = datetime.now(tz=IST)
        today_date = now_ist.date()
        if self._morning_briefing_date == today_date:
            return

        mode_label = "LIVE" if self._s.live_order_placement else "PAPER"
        available_cash: float | None = None
        try:
            margins = self._kite.margins(segment="commodity")
            avail = margins.get("available", {})
            available_cash = float(
                avail.get("live_balance") or avail.get("cash") or 0.0
            )
        except Exception as exc:
            logger.debug("Could not fetch commodity margins: %s", exc)

        uptime_str = self._get_uptime_str()
        system_info = self._get_system_info()

        self._notifier.morning_briefing(
            contract=self._active_symbol,
            capital=self._s.capital,
            available_cash=available_cash,
            candle_interval=self._s.candle_interval_minutes,
            entry_cutoff=self._s.entry_cutoff,
            eod_exit=self._s.eod_exit,
            mode=mode_label,
            uptime=uptime_str,
            system_info=system_info,
        )
        self._morning_briefing_date = today_date

        # If engine started late (after heartbeat time), mark heartbeat sent so we don't double ping
        hb_h, hb_m = self._s.heartbeat_hhmm
        if now_ist.time() >= time(hb_h, hb_m):
            self._heartbeat_date = today_date

    def _send_heartbeat(self, now_ist: datetime) -> None:
        today_date = now_ist.date()
        if self._heartbeat_date == today_date:
            return

        uptime_str = self._get_uptime_str()
        ltp = self._fetch_current_ltp()
        pos = self._signal_engine.position or "FLAT"

        self._notifier.heartbeat(
            contract=self._active_symbol,
            ltp=ltp,
            position=pos,
            uptime=uptime_str,
            candles_evaluated=self._candles_today,
            system_info=self._get_system_info(),
        )
        self._heartbeat_date = today_date
        logger.info(
            "Sent daily heartbeat health ping (uptime: %s, position: %s, candles: %d).",
            uptime_str, pos, self._candles_today,
        )

    def _send_eod_summary(self, now_ist: datetime) -> None:
        date_str = now_ist.strftime("%Y-%m-%d")
        dow = now_ist.weekday()
        next_session = "Monday 09:00 IST" if dow == 4 else "tomorrow 09:00 IST"
        self._notifier.eod_summary(
            date_str=date_str,
            trades_taken=self._signal_engine.trade_today,
            candles_evaluated=self._candles_today,
            next_session=next_session,
        )
        self._candles_today = 0
        self._signal_engine.trade_today = 0

    def _standby_sleep(self) -> None:
        """Sleep in 60s increments during off-market hours."""
        logger.info("Low-power standby. Checking market status every 60s…")
        for _ in range(60):
            if not self._running:
                return
            _time.sleep(1)
