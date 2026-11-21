"""
src/signal_engine.py
====================
Translates the gold.pine PineScript strategy into Python bar-by-bar signal evaluation.

Gold Trend Filter — Strategy Rules (from gold-settings.png + gold.pine):
  Enter Channel    : 20 (highest high lookback)
  Exit Channel     : 10 (lowest low lookback)
  Trade Mode       : Both (Long and Short)
  EMA Length       : 200 (trend filter on HA close)
  ATR Period       : 20 (EMA of True Range)
  ATR TP Multiplier: 4.0  (take profit — NOT used for lot sizing)
  ATR Trail Mult   : 2.0  (trailing stop loss)
  Partial TP       : DISABLED
  Min Long Days    : 0  (no minimum hold before short)
  Skip Opening     : YES — skip first 2 candles of each MCX session

Entry Conditions:
  LONG  : ha_close >= upper[1] AND ha_close > ema_trend AND no open position
  SHORT : ha_close <= lower[1] AND ha_close < ema_trend AND no open position
          AND longHeldEnough (if min_long_days > 0)

Exit Conditions (LONG):
  - ha_close <= lower[1]       (channel stop)
  - Trailing SL hit

Exit Conditions (SHORT):
  - ha_close >= upper[1]       (channel stop)
  - Trailing SL hit

Trailing SL:
  LONG  trail = running max of (ha_close - ATR * 2.0)
  SHORT trail = running min of (ha_close + ATR * 2.0)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    """Returned by SignalEngine.evaluate() for each candle."""
    action: str                     # "NONE" | "BUY" | "SELL" | "EXIT_BUY" | "EXIT_SELL"
    exit_reason: str = ""           # human-readable reason for exits
    entry_price: float = 0.0       # ha_close at signal bar
    trail_sl: Optional[float] = None
    atr: float = 0.0
    candle_time: Optional[datetime] = None


class SignalEngine:
    """
    Bar-by-bar strategy evaluator.

    State is maintained across candle callbacks to track:
      - current open position ("LONG" | "SHORT" | None)
      - trailing stop levels
      - bar counts for duration filter and opening candle filter
    """

    def __init__(self, settings) -> None:
        self._s = settings

        # Position state
        self._position: str | None = None      # "LONG" | "SHORT" | None
        self._entry_bar: int = 0
        self._bar_index: int = 0

        # Trailing stop state
        self._long_trail: float | None = None
        self._short_trail: float | None = None

        # Long hold duration tracking (for min_long_days filter)
        self._long_entry_bar: int | None = None
        self._last_long_bars: int = 0           # bars held in last completed long

        # Daily candle counter (for skip-opening-candles filter)
        self._current_session_date: object = None
        self._day_bar_count: int = 0

        # Stats
        self.trade_today: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def restore_state(
        self,
        position: str | None,
        long_trail: float | None = None,
        short_trail: float | None = None,
        entry_bar: int = 0,
    ) -> None:
        """Restore state from StateManager on startup reconciliation."""
        self._position    = position
        self._long_trail  = long_trail
        self._short_trail = short_trail
        self._entry_bar   = entry_bar
        logger.info(
            "SignalEngine state restored: position=%s long_trail=%s short_trail=%s",
            position, long_trail, short_trail,
        )

    def evaluate(self, row, candle_time: datetime) -> SignalResult:
        """
        Evaluate one closed Heikin-Ashi candle.

        Parameters
        ----------
        row : dict or pd.Series
            Must contain: ha_close, ha_high, ha_low, upper, lower, ema_trend, atr.
        candle_time : datetime
            Candle open timestamp in IST.

        Returns
        -------
        SignalResult
        """
        self._bar_index += 1

        ha_close   = float(row["ha_close"])
        ha_high    = float(row["ha_high"])
        ha_low     = float(row["ha_low"])
        upper      = float(row["upper"])   # already shifted by 1 (previous bar's channel)
        lower      = float(row["lower"])
        ema_trend  = float(row["ema_trend"])
        atr        = float(row["atr"])

        # ----- Skip-opening-candles filter -----
        self._update_day_bar_count(candle_time)
        entry_allowed_today = self._is_entry_allowed_today(candle_time)

        # ----- Entry cutoff (no new entries near close) -----
        entry_allowed_time = self._is_within_entry_window(candle_time)
        can_enter = entry_allowed_today and entry_allowed_time

        # ----- Update trailing SL -----
        self._update_trailing_sl(ha_close, atr)

        # ----- Check exits first (position management) -----
        if self._position == "LONG":
            result = self._check_long_exit(ha_close, lower, atr, candle_time)
            if result.action != "NONE":
                self._close_position()
                return result

        elif self._position == "SHORT":
            result = self._check_short_exit(ha_close, upper, atr, candle_time)
            if result.action != "NONE":
                self._close_position()
                return result

        # ----- Check entry conditions -----
        if self._position is None and can_enter:

            # Long condition: HA close breaks above upper channel AND above EMA
            long_ok = (
                self._s.allow_long
                and ha_close >= upper
            )
            if long_ok and ha_close > ema_trend:
                self._open_long(atr, ha_close)
                self.trade_today += 1
                logger.info(
                    "BUY signal: bar=%d time=%s close=%.2f upper=%.2f ema=%.2f atr=%.2f trail=%.2f",
                    self._bar_index, candle_time.strftime("%H:%M"),
                    ha_close, upper, ema_trend, atr, self._long_trail or 0,
                )
                return SignalResult(
                    action="BUY",
                    entry_price=ha_close,
                    trail_sl=self._long_trail,
                    atr=atr,
                    candle_time=candle_time,
                )

            # Short condition: HA close breaks below lower channel AND below EMA
            # AND long_held_enough (if min_long_days > 0)
            short_ok = (
                self._s.allow_short
                and ha_close <= lower
                and ha_close < ema_trend
                and self._long_held_enough()
            )
            if short_ok:
                self._open_short(atr, ha_close)
                self.trade_today += 1
                logger.info(
                    "SELL signal: bar=%d time=%s close=%.2f lower=%.2f ema=%.2f atr=%.2f trail=%.2f",
                    self._bar_index, candle_time.strftime("%H:%M"),
                    ha_close, lower, ema_trend, atr, self._short_trail or 0,
                )
                return SignalResult(
                    action="SELL",
                    entry_price=ha_close,
                    trail_sl=self._short_trail,
                    atr=atr,
                    candle_time=candle_time,
                )

        return SignalResult(action="NONE", atr=atr, candle_time=candle_time)

    def force_exit(self, reason: str, candle_time: datetime, atr: float) -> SignalResult:
        """
        Force an immediate exit (EOD or external trigger).
        Called by SessionManager watchdog regardless of signal conditions.
        """
        if self._position is None:
            return SignalResult(action="NONE", candle_time=candle_time)

        action = "EXIT_BUY" if self._position == "LONG" else "EXIT_SELL"
        logger.info("Force exit: %s reason=%s", action, reason)
        self._close_position()
        return SignalResult(
            action=action,
            exit_reason=reason,
            trail_sl=None,
            atr=atr,
            candle_time=candle_time,
        )

    @property
    def position(self) -> str | None:
        return self._position

    @property
    def long_trail(self) -> float | None:
        return self._long_trail

    @property
    def short_trail(self) -> float | None:
        return self._short_trail

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_trailing_sl(self, ha_close: float, atr: float) -> None:
        """Update running trailing stop levels."""
        # Reset trails when flat and no pending signals
        if self._position is None:
            self._long_trail  = None
            self._short_trail = None
            return

        if self._position == "LONG":
            new_trail = ha_close - atr * self._s.atr_trail_multiplier
            if self._long_trail is None:
                self._long_trail = new_trail
            else:
                self._long_trail = max(self._long_trail, new_trail)

        elif self._position == "SHORT":
            new_trail = ha_close + atr * self._s.atr_trail_multiplier
            if self._short_trail is None:
                self._short_trail = new_trail
            else:
                self._short_trail = min(self._short_trail, new_trail)

    def _check_long_exit(
        self, ha_close: float, lower: float, atr: float, candle_time: datetime
    ) -> SignalResult:
        """Check all LONG exit conditions."""
        # Channel stop: close drops below exit channel lower
        if ha_close <= lower:
            return SignalResult(
                action="EXIT_BUY",
                exit_reason="Channel Stop (close ≤ lower)",
                trail_sl=self._long_trail,
                atr=atr,
                candle_time=candle_time,
            )

        # Trailing SL hit
        if self._long_trail is not None and ha_close <= self._long_trail:
            return SignalResult(
                action="EXIT_BUY",
                exit_reason=f"Trailing SL Hit (trail={self._long_trail:.2f})",
                trail_sl=self._long_trail,
                atr=atr,
                candle_time=candle_time,
            )

        return SignalResult(action="NONE")

    def _check_short_exit(
        self, ha_close: float, upper: float, atr: float, candle_time: datetime
    ) -> SignalResult:
        """Check all SHORT exit conditions."""
        # Channel stop: close breaks above enter channel upper
        if ha_close >= upper:
            return SignalResult(
                action="EXIT_SELL",
                exit_reason="Channel Stop (close ≥ upper)",
                trail_sl=self._short_trail,
                atr=atr,
                candle_time=candle_time,
            )

        # Trailing SL hit
        if self._short_trail is not None and ha_close >= self._short_trail:
            return SignalResult(
                action="EXIT_SELL",
                exit_reason=f"Trailing SL Hit (trail={self._short_trail:.2f})",
                trail_sl=self._short_trail,
                atr=atr,
                candle_time=candle_time,
            )

        return SignalResult(action="NONE")

    def _open_long(self, atr: float, ha_close: float = 0.0) -> None:
        self._position         = "LONG"
        self._long_entry_bar   = self._bar_index
        self._entry_bar        = self._bar_index
        # Initialize trail immediately at entry so exit check on next bar works correctly
        self._long_trail       = ha_close - atr * self._s.atr_trail_multiplier if ha_close > 0 else None
        self._short_trail      = None

    def _open_short(self, atr: float, ha_close: float = 0.0) -> None:
        self._position    = "SHORT"
        self._entry_bar   = self._bar_index
        self._long_trail  = None
        # Initialize trail immediately at entry close so the first exit bar can hit it
        self._short_trail = ha_close + atr * self._s.atr_trail_multiplier if ha_close > 0 else None

    def _close_position(self) -> None:
        # Track how long the last long was held (for min_long_days filter)
        if self._position == "LONG" and self._long_entry_bar is not None:
            self._last_long_bars = self._bar_index - self._long_entry_bar
        self._position    = None
        self._long_trail  = None
        self._short_trail = None

    def _long_held_enough(self) -> bool:
        """Check if the minimum long duration requirement is satisfied."""
        if self._s.min_long_bars == 0:
            return True
        return self._last_long_bars >= self._s.min_long_bars

    def _update_day_bar_count(self, candle_time: datetime) -> None:
        """Track bar index within each MCX session day."""
        c_date = candle_time.date()
        if c_date != self._current_session_date:
            self._current_session_date = c_date
            self._day_bar_count = 1
        else:
            self._day_bar_count += 1

    def _is_entry_allowed_today(self, candle_time: datetime) -> bool:
        """
        Returns False for the first N candles of the MCX session.
        Implements PineScript's enableSkipFirstBars / barsToSkip logic.
        """
        if not self._s.skip_opening_candles:
            return True
        # Allowed if bar count exceeds the skip threshold
        return self._day_bar_count > self._s.bars_to_skip

    def _is_within_entry_window(self, candle_time: datetime) -> bool:
        """Returns False after entry_cutoff time (no new entries near close)."""
        cutoff_h, cutoff_m = self._s.entry_cutoff_hhmm
        t = candle_time.time()
        return t <= time(cutoff_h, cutoff_m)
