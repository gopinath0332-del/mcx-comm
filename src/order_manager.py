"""
src/order_manager.py
====================
Places MCX Gold Petal futures orders via Kite Connect.

Handles:
  - BUY (Long entry)   : Market order, NRML product
  - SELL (Short entry) : Market order, NRML product
  - EXIT BUY / EXIT SELL: Market exit order
  - Simulation mode   : No real orders; logs and notifies as if live

MCX Futures use:
  - Exchange: MCX
  - Product:  NRML  (overnight carry — NOT MIS)
  - Order type: MARKET
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


class OrderManager:
    """
    Manages MCX futures order placement and position tracking.

    Parameters
    ----------
    kite : KiteConnect
        Authenticated Kite instance.
    notifier : DiscordNotifier
        For sending trade alerts.
    state_manager : StateManager
        For persisting open position state.
    settings : Settings
        Application settings.
    lot_sizer : LotSizer
        For calculating position size.
    """

    EXCHANGE = "MCX"
    PRODUCT  = KiteConnect.PRODUCT_NRML
    ORDER_TYPE = KiteConnect.ORDER_TYPE_MARKET

    def __init__(
        self,
        kite,
        notifier,
        state_manager,
        settings,
        lot_sizer,
    ) -> None:
        self._kite          = kite
        self._notifier      = notifier
        self._state         = state_manager
        self._s             = settings
        self._sizer         = lot_sizer

        # Active position tracking
        self._symbol: str | None   = None
        self._instrument_token: int = 0
        self._position: str | None = None   # "LONG" | "SHORT" | None
        self._qty: int             = 0
        self._entry_price: float   = 0.0

    # ------------------------------------------------------------------
    # State restoration (from StateManager.reconcile)
    # ------------------------------------------------------------------

    def restore_state(
        self,
        symbol: str,
        qty: int,
        position: str,
        entry_price: float,
    ) -> None:
        self._symbol      = symbol
        self._qty         = qty
        self._position    = position
        self._entry_price = entry_price
        logger.info("OrderManager state restored: %s %s qty=%d", position, symbol, qty)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def open_symbol(self) -> str | None:
        return self._symbol

    @property
    def open_position(self) -> str | None:
        return self._position

    @property
    def open_qty(self) -> int:
        return self._qty

    # ------------------------------------------------------------------
    # Entry handlers
    # ------------------------------------------------------------------

    def handle_buy(
        self,
        symbol: str,
        instrument_token: int,
        signal_result,
        candle_time: datetime,
    ) -> None:
        """Place a LONG entry order."""
        atr = signal_result.atr
        qty = self._sizer.calculate(atr)
        sizing_desc = self._sizer.describe(atr)

        order_id = self._place_order(
            symbol=symbol,
            transaction_type=KiteConnect.TRANSACTION_TYPE_BUY,
            qty=qty,
        )

        self._symbol           = symbol
        self._instrument_token = instrument_token
        self._position         = "LONG"
        self._qty              = qty
        self._entry_price      = signal_result.entry_price

        # Persist state
        from src.state_manager import PositionState
        self._state.save(PositionState(
            position="LONG",
            symbol=symbol,
            instrument_token=instrument_token,
            entry_price=signal_result.entry_price,
            qty=qty,
            long_trail=signal_result.trail_sl,
            short_trail=None,
            entry_timestamp=candle_time.isoformat(),
        ))

        # Notify Discord
        self._notifier.buy_entry(
            symbol=symbol,
            entry_price=signal_result.entry_price,
            qty=qty,
            atr=atr,
            trail_sl=signal_result.trail_sl,
            candle_time=candle_time,
            sizing_desc=sizing_desc,
            order_id=order_id,
            live=self._s.live_order_placement,
        )
        logger.info(
            "BUY ORDER: %s qty=%d entry=%.2f trail=%.2f order_id=%s live=%s",
            symbol, qty, signal_result.entry_price,
            signal_result.trail_sl or 0, order_id, self._s.live_order_placement,
        )

    def handle_sell(
        self,
        symbol: str,
        instrument_token: int,
        signal_result,
        candle_time: datetime,
    ) -> None:
        """Place a SHORT entry order."""
        atr = signal_result.atr
        qty = self._sizer.calculate(atr)
        sizing_desc = self._sizer.describe(atr)

        order_id = self._place_order(
            symbol=symbol,
            transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
            qty=qty,
        )

        self._symbol           = symbol
        self._instrument_token = instrument_token
        self._position         = "SHORT"
        self._qty              = qty
        self._entry_price      = signal_result.entry_price

        from src.state_manager import PositionState
        self._state.save(PositionState(
            position="SHORT",
            symbol=symbol,
            instrument_token=instrument_token,
            entry_price=signal_result.entry_price,
            qty=qty,
            long_trail=None,
            short_trail=signal_result.trail_sl,
            entry_timestamp=candle_time.isoformat(),
        ))

        self._notifier.sell_entry(
            symbol=symbol,
            entry_price=signal_result.entry_price,
            qty=qty,
            atr=atr,
            trail_sl=signal_result.trail_sl,
            candle_time=candle_time,
            sizing_desc=sizing_desc,
            order_id=order_id,
            live=self._s.live_order_placement,
        )
        logger.info(
            "SELL ORDER: %s qty=%d entry=%.2f trail=%.2f order_id=%s live=%s",
            symbol, qty, signal_result.entry_price,
            signal_result.trail_sl or 0, order_id, self._s.live_order_placement,
        )

    # ------------------------------------------------------------------
    # Exit handler
    # ------------------------------------------------------------------

    def handle_exit(
        self,
        signal_result,
        candle_time: datetime,
        exit_price: float = 0.0,
    ) -> None:
        """Exit the current open position (long or short)."""
        if self._position is None or self._symbol is None:
            return

        is_long = self._position == "LONG"
        transaction_type = (
            KiteConnect.TRANSACTION_TYPE_SELL if is_long
            else KiteConnect.TRANSACTION_TYPE_BUY
        )

        order_id = self._place_order(
            symbol=self._symbol,
            transaction_type=transaction_type,
            qty=self._qty,
        )

        pnl_est = self._estimate_pnl(exit_price, is_long)

        self._notifier.exit_trade(
            symbol=self._symbol,
            position=self._position,
            exit_price=exit_price,
            entry_price=self._entry_price,
            qty=self._qty,
            reason=signal_result.exit_reason,
            pnl_est=pnl_est,
            candle_time=candle_time,
            order_id=order_id,
            live=self._s.live_order_placement,
        )
        logger.info(
            "EXIT ORDER: %s %s qty=%d exit=%.2f entry=%.2f pnl_est=%.2f reason=%s",
            "EXIT_BUY" if is_long else "EXIT_SELL",
            self._symbol, self._qty, exit_price, self._entry_price,
            pnl_est, signal_result.exit_reason,
        )

        # Clear state
        self._state.clear()
        self._symbol    = None
        self._position  = None
        self._qty       = 0
        self._entry_price = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _place_order(
        self,
        symbol: str,
        transaction_type: str,
        qty: int,
    ) -> str:
        """
        Place a market order on MCX. Returns order_id string.
        In simulation mode, returns "SIM-<symbol>" without hitting Kite.
        """
        if not self._s.live_order_placement:
            sim_id = f"SIM-{symbol}-{transaction_type}"
            logger.info("[SIMULATION] Would place: %s %s qty=%d", transaction_type, symbol, qty)
            return sim_id

        try:
            order_id = self._kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=self.EXCHANGE,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=qty,
                product=self.PRODUCT,
                order_type=self.ORDER_TYPE,
            )
            logger.info(
                "Order placed: %s %s qty=%d order_id=%s",
                transaction_type, symbol, qty, order_id,
            )
            return str(order_id)
        except Exception as exc:
            logger.error(
                "Order placement failed: %s %s qty=%d error=%s",
                transaction_type, symbol, qty, exc,
            )
            self._notifier.error(
                f"❌ Order placement FAILED: {transaction_type} {symbol} qty={qty}\n"
                f"Error: {exc}"
            )
            return "FAILED"

    def _estimate_pnl(self, exit_price: float, is_long: bool) -> float:
        """
        Rough P&L estimate based on entry vs exit price.
        GOLDPETAL lot size = 1 gram, so PnL = price_diff × qty × lot_multiplier.
        """
        if exit_price <= 0 or self._entry_price <= 0:
            return 0.0
        diff = exit_price - self._entry_price
        if not is_long:
            diff = -diff
        return round(diff * self._qty * self._s.lot_multiplier, 2)
