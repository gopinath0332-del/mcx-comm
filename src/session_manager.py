"""
src/session_manager.py
=======================
Top-level orchestrator for the MCX Gold commodity trading engine.

Data flow:
  Kite WS tick
    └─► CandleBuilder.on_tick()
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
import threading
import time as _time
from collections import deque
from datetime import datetime, time, timedelta

import pandas as pd

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

from kiteconnect import KiteTicker

from config.settings import Settings
from src.candle_builder import CandleBuilder, HeikinAshiConverter
from src.contract_selector import ContractSelector
from src.discord_notifier import DiscordNotifier
from src.indicators import apply_all
from src.lot_sizer import LotSizer
from src.order_manager import OrderManager
from src.signal_engine import SignalEngine
from src.state_manager import StateManager

logger = logging.getLogger(__name__)

# Minimum candles in buffer before signals are evaluated
# (needs at least enter_channel=20 candles for valid indicators)
MIN_CANDLES = 25


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
    Manages a full MCX Gold intraday trading session.

    Parameters
    ----------
    settings : Settings
        Loaded application settings.
    kite_client : KiteConnect | None
        Injected authenticated Kite client (useful for testing).
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

        # Candle infrastructure
        self._candle_builder = CandleBuilder(
            interval_minutes=settings.candle_interval_minutes
        )
        self._candle_buffer: deque[dict] = deque(maxlen=500)

        # WebSocket
        self._ticker: KiteTicker | None = None
        self._running: bool             = False
        self._last_tick_time: float | None = None
        self._last_reconnect_attempt: float = 0.0
        self._last_ltp: float = 0.0

        # Session stats
        self._candles_today: int = 0
        self._session_date       = datetime.now(tz=IST).date()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main entry point. Enforces MCX market hours."""
        mode = "LIVE" if self._s.live_order_placement else "PAPER"
        logger.info("MCX Gold Engine starting in %s mode.", mode)
        self._running = True

        # 1. Select contract (respects open position rule)
        open_sym = self._state_mgr.load()
        open_sym_str = open_sym.symbol if open_sym else None
        self._active_symbol, self._active_token = (
            self._contract_selector.get_active_contract(self._kite, open_sym_str)
        )
        logger.info("Active contract: %s (token=%d)", self._active_symbol, self._active_token)

        # 2. Reconcile state with broker
        self._state_mgr.reconcile(
            kite=self._kite,
            signal_engine=self._signal_engine,
            order_manager=self._order_mgr,
            live_mode=self._s.live_order_placement,
        )

        # 3. Pre-seed historical candles
        self._preseed_historical_candles()

        if not self._s.enforce_market_hours:
            logger.info("Market hours enforcement disabled. Connecting directly.")
            self._start_active_session()
            return

        now_ist = datetime.now(tz=IST)

        if _is_weekend(now_ist):
            day_name = now_ist.strftime("%A")
            logger.info("⏸️ %s (Weekend) — MCX closed. Entering standby.", day_name)
            self._notifier.info(
                f"⏸️ **MCX Gold Engine — Weekend Standby**\n"
                f"• Today is **{day_name}** — MCX is closed.\n"
                f"• Engine is sleeping, will auto-connect on Monday 09:00 IST."
            )
            self._standby_loop()
            return

        if not _is_market_hours(now_ist, self._s.market_open_hhmm, self._s.market_close_hhmm):
            logger.info(
                "⏸️ Outside MCX hours (%s IST). Entering standby.",
                now_ist.strftime("%H:%M"),
            )
            self._standby_loop()
            return

        self._start_active_session()

    def _start_active_session(self) -> None:
        """Start WebSocket feed and watchdog for an active MCX session."""
        if len(self._candle_buffer) < MIN_CANDLES:
            self._preseed_historical_candles()
        self._send_morning_briefing()
        threading.Thread(
            target=self._session_watchdog, name="MCXWatchdog", daemon=True
        ).start()
        self._start_ticker()

    # ------------------------------------------------------------------
    # Historical pre-seed
    # ------------------------------------------------------------------

    def _preseed_historical_candles(self) -> int:
        """
        Pre-load recent 1H OHLCV candles from Kite historical API into buffer.
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

            count = self._s.warmup_candles
            recent = records[-count:] if len(records) > count else records
            self._candle_buffer.clear()
            for rec in recent:
                rec_dt = rec.get("date")
                if hasattr(rec_dt, "tzinfo") and rec_dt.tzinfo:
                    ist_ts = rec_dt.astimezone(IST)
                elif hasattr(rec_dt, "replace"):
                    ist_ts = rec_dt.replace(tzinfo=IST)
                else:
                    ist_ts = now_ist
                self._candle_buffer.append({
                    "open":      float(rec.get("open",   0.0)),
                    "high":      float(rec.get("high",   0.0)),
                    "low":       float(rec.get("low",    0.0)),
                    "close":     float(rec.get("close",  0.0)),
                    "volume":    float(rec.get("volume", 0.0)),
                    "timestamp": ist_ts,
                    "date":      ist_ts.date(),
                })
            logger.info(
                "Pre-seeded %d historical 60minute candles for %s.",
                len(self._candle_buffer), self._active_symbol,
            )
            return len(self._candle_buffer)
        except Exception as exc:
            logger.warning("Could not pre-seed historical candles: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Morning briefing & EOD summary
    # ------------------------------------------------------------------

    def _send_morning_briefing(self) -> None:
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

        self._notifier.morning_briefing(
            contract=self._active_symbol,
            capital=self._s.capital,
            available_cash=available_cash,
            candle_interval=self._s.candle_interval_minutes,
            entry_cutoff=self._s.entry_cutoff,
            eod_exit=self._s.eod_exit,
            mode=mode_label,
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

    # ------------------------------------------------------------------
    # Standby loop
    # ------------------------------------------------------------------

    def _standby_loop(self) -> None:
        """Sleep in 60s increments until MCX market opens."""
        logger.info("Low-power standby. Checking market status every 60s…")
        while self._running:
            now_ist = datetime.now(tz=IST)
            if _is_market_hours(now_ist, self._s.market_open_hhmm, self._s.market_close_hhmm):
                logger.info(
                    "Market session now active (%s IST). Starting WebSocket…",
                    now_ist.strftime("%H:%M:%S"),
                )
                self._start_active_session()
                return
            for _ in range(60):
                if not self._running:
                    return
                _time.sleep(1)

    # ------------------------------------------------------------------
    # Session watchdog
    # ------------------------------------------------------------------

    def _session_watchdog(self) -> None:
        """
        Background thread:
          1. Force-exit open positions at eod_exit time
          2. Detect stale WebSocket (>90s no ticks)
          3. Send EOD summary and shutdown at market close
        """
        logger.info(
            "MCX Watchdog armed: EOD exit at %s IST | Market close at %s IST.",
            self._s.eod_exit, self._s.market_close,
        )
        eod_h, eod_m = self._s.eod_exit_hhmm
        close_h, close_m = self._s.market_close_hhmm
        eod_target   = time(eod_h, eod_m)
        close_target = time(close_h, close_m)

        while self._running:
            try:
                now_ist = datetime.now(tz=IST)
                now_t   = now_ist.time()

                # 1. EOD force-exit
                if now_t >= eod_target and self._order_mgr.open_symbol is not None:
                    logger.warning(
                        "🚨 [EOD WATCHDOG] Time %s ≥ %s with open position %s. Force exiting.",
                        now_ist.strftime("%H:%M:%S"), self._s.eod_exit,
                        self._order_mgr.open_symbol,
                    )
                    from src.signal_engine import SignalResult
                    result = SignalResult(
                        action="EXIT_BUY" if self._signal_engine.position == "LONG" else "EXIT_SELL",
                        exit_reason=f"EOD Force Exit {self._s.eod_exit}",
                        candle_time=now_ist,
                        atr=0.0,
                    )
                    self._order_mgr.handle_exit(result, now_ist, exit_price=self._last_ltp)
                    self._signal_engine.force_exit(
                        reason="EOD Watchdog", candle_time=now_ist, atr=0.0
                    )

                # 2. WebSocket heartbeat guard (>90s without ticks during session)
                open_h, open_m = self._s.market_open_hhmm
                if time(open_h, open_m) <= now_t < close_target:
                    if self._ticker and self._last_tick_time is not None:
                        stale = _time.time() - self._last_tick_time
                        if stale > 90.0:
                            since_reconnect = _time.time() - self._last_reconnect_attempt
                            if since_reconnect > 90.0:
                                self._last_reconnect_attempt = _time.time()
                                logger.warning(
                                    "⚠️ No ticks for %ds — recovering stale WebSocket.",
                                    int(stale),
                                )
                                self._notifier.info(
                                    f"⚠️ **WebSocket Stalled** — no ticks for {int(stale)}s.\n"
                                    "Attempting connection reset."
                                )
                                self._recover_stale_websocket()

                # 3. Market close — EOD summary + shutdown
                if now_t >= close_target:
                    logger.info("MCX Market close (%s IST). Sending EOD summary.", self._s.market_close)
                    self._send_eod_summary(now_ist)
                    self.stop()
                    break

            except Exception as exc:
                logger.error("Watchdog error: %s", exc)
            _time.sleep(5)

    def _recover_stale_websocket(self) -> None:
        try:
            if self._ticker:
                ws = getattr(self._ticker, "ws", None)
                if ws and hasattr(ws, "dropConnection"):
                    ws.dropConnection(abort=True)
                elif hasattr(self._ticker, "resubscribe"):
                    self._ticker.resubscribe()
        except Exception as exc:
            logger.error("Error recovering stale WebSocket: %s", exc)

    def stop(self) -> None:
        """Gracefully stop ticker and exit."""
        logger.info("Stopping MCX session manager…")
        self._running = False
        if self._ticker:
            try:
                self._ticker.stop()
            except Exception:
                pass
        self._notifier.info("⏹️ MCX Gold Engine stopped.")
        self._notifier.stop()

    # ------------------------------------------------------------------
    # Kite WebSocket
    # ------------------------------------------------------------------

    def _start_ticker(self) -> None:
        self._ticker = KiteTicker(
            api_key=self._s.kite_api_key,
            access_token=self._s.kite_access_token,
        )
        self._ticker.on_connect     = self._on_connect
        self._ticker.on_ticks       = self._on_ticks
        self._ticker.on_close       = self._on_close
        self._ticker.on_error       = self._on_error
        self._ticker.on_reconnect   = self._on_reconnect
        self._ticker.on_noreconnect = self._on_noreconnect
        self._ticker.connect(threaded=False)   # blocking

    def _on_connect(self, ws, response) -> None:
        logger.info(
            "WebSocket connected. Subscribing to %s (token=%d).",
            self._active_symbol, self._active_token,
        )
        self._ticker.subscribe([self._active_token])
        self._ticker.set_mode(self._ticker.MODE_FULL, [self._active_token])

    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        self._last_tick_time = _time.time()
        for tick in ticks:
            if tick.get("instrument_token") != self._active_token:
                continue
            ltp = float(tick.get("last_price", 0.0))
            vol = float(tick.get("volume_traded", 0.0))
            ts  = tick.get("timestamp") or datetime.now(tz=IST)
            if ltp <= 0:
                continue
            self._last_ltp = ltp
            closed = self._candle_builder.on_tick(ltp, vol, ts)
            if closed:
                candle = self._candle_builder.last_closed_candle
                threading.Thread(
                    target=self._process_closed_candle,
                    args=(candle,),
                    daemon=True,
                ).start()

    def _on_close(self, ws, code, reason) -> None:
        logger.warning("WebSocket closed: code=%s reason=%s", code, reason)

    def _on_error(self, ws, code, reason) -> None:
        logger.error("WebSocket error: code=%s reason=%s", code, reason)
        self._notifier.error(f"WebSocket error (code={code}): {reason}")

    def _on_reconnect(self, ws, attempts_count) -> None:
        logger.warning("WebSocket reconnecting (attempt %d)…", attempts_count)

    def _on_noreconnect(self, ws) -> None:
        logger.critical("WebSocket exhausted reconnect attempts. Shutting down.")
        self._notifier.error("⚠️ WebSocket lost connection permanently. Engine offline.")
        self._running = False

    # ------------------------------------------------------------------
    # Candle processing pipeline
    # ------------------------------------------------------------------

    def _process_closed_candle(self, candle) -> None:
        """
        Called in a background thread each time a 1H candle closes.
        Runs HA conversion, indicators, and signal evaluation.
        """
        try:
            ist_ts = candle.timestamp.astimezone(IST)

            # Track daily stats
            c_date = ist_ts.date()
            if c_date != self._session_date:
                self._candles_today  = 0
                self._session_date   = c_date
            self._candles_today += 1

            # Add to rolling buffer
            self._candle_buffer.append({
                "open":      candle.open,
                "high":      candle.high,
                "low":       candle.low,
                "close":     candle.close,
                "volume":    candle.volume,
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

            logger.debug(
                "Candle %s | HA_C=%.2f upper=%.2f lower=%.2f ema=%.2f atr=%.2f",
                ist_ts.strftime("%H:%M"),
                last["ha_close"], last["upper"], last["lower"],
                last["ema_trend"], last["atr"],
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
            logger.error("Error processing candle at %s: %s", candle.timestamp, exc, exc_info=True)
            self._notifier.error(f"Candle processing error at {candle.timestamp}: {exc}")
