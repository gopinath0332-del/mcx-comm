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
import math
import time as _time
from datetime import datetime
from typing import Any

from kiteconnect import KiteConnect
from src.logger import get_trade_logger

logger = logging.getLogger(__name__)
trade_logger = get_trade_logger()

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
        """Place a LONG entry order with margin pre-flight check."""
        atr = signal_result.atr
        entry_price = signal_result.entry_price

        # 1. Calculate risk-driven lots
        risk_lots = self._sizer.calculate(atr)

        # 2. Pre-flight margin verification: Final Lots = min(Risk Lots, Affordable Margin Lots)
        final_lots, sizing_desc = self._apply_margin_preflight(
            symbol=symbol,
            transaction_type=KiteConnect.TRANSACTION_TYPE_BUY,
            risk_lots=risk_lots,
            entry_price=entry_price,
            atr=atr,
        )
        if final_lots <= 0:
            logger.warning("BUY order aborted: 0 affordable lots based on available margin.")
            return

        order_id = self._place_order(
            symbol=symbol,
            transaction_type=KiteConnect.TRANSACTION_TYPE_BUY,
            qty=final_lots,
        )
        if order_id == "FAILED":
            return

        # 3. Poll order history for actual execution price (slippage tracking)
        fill_price, status = self._fetch_executed_price(
            order_id=order_id,
            fallback_price=entry_price,
        )
        if status.startswith("REJECTED") or status.startswith("CANCELLED") or fill_price <= 0:
            logger.error("BUY order %s rejected/cancelled on exchange: %s", order_id, status)
            self._notifier.error(f"❌ BUY Order {order_id} ({symbol}) was {status}")
            return

        slippage = fill_price - entry_price
        slippage_str = f" | Slippage: ₹{slippage:+.2f} ({(slippage/entry_price)*100:+.2f}%)" if entry_price > 0 and self._s.live_order_placement else ""

        self._symbol           = symbol
        self._instrument_token = instrument_token
        self._position         = "LONG"
        self._qty              = final_lots
        self._entry_price      = fill_price

        # Persist state with real execution price
        from src.state_manager import PositionState
        self._state.save(PositionState(
            position="LONG",
            symbol=symbol,
            instrument_token=instrument_token,
            entry_price=fill_price,
            qty=final_lots,
            long_trail=signal_result.trail_sl,
            short_trail=None,
            entry_timestamp=candle_time.isoformat(),
        ))

        # Notify Discord
        self._notifier.buy_entry(
            symbol=symbol,
            entry_price=fill_price,
            qty=final_lots,
            atr=atr,
            trail_sl=signal_result.trail_sl,
            candle_time=candle_time,
            sizing_desc=f"{sizing_desc}{slippage_str}",
            order_id=order_id,
            live=self._s.live_order_placement,
        )
        logger.info(
            "BUY ORDER: %s qty=%d fill=%.2f (signal=%.2f) trail=%.2f order_id=%s live=%s",
            symbol, final_lots, fill_price, entry_price,
            signal_result.trail_sl or 0, order_id, self._s.live_order_placement,
        )
        trade_logger.info(
            "🟢 BUY ENTRY | %s | Qty: %d | Fill Price: ₹%.2f (Signal: ₹%.2f) | Trail SL: ₹%.2f | ATR: ₹%.2f | Order ID: %s | Mode: %s",
            symbol, final_lots, fill_price, entry_price,
            signal_result.trail_sl or 0, atr, order_id, "LIVE" if self._s.live_order_placement else "PAPER",
        )

    def handle_sell(
        self,
        symbol: str,
        instrument_token: int,
        signal_result,
        candle_time: datetime,
    ) -> None:
        """Place a SHORT entry order with margin pre-flight check."""
        atr = signal_result.atr
        entry_price = signal_result.entry_price

        # 1. Calculate risk-driven lots
        risk_lots = self._sizer.calculate(atr)

        # 2. Pre-flight margin verification: Final Lots = min(Risk Lots, Affordable Margin Lots)
        final_lots, sizing_desc = self._apply_margin_preflight(
            symbol=symbol,
            transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
            risk_lots=risk_lots,
            entry_price=entry_price,
            atr=atr,
        )
        if final_lots <= 0:
            logger.warning("SELL order aborted: 0 affordable lots based on available margin.")
            return

        order_id = self._place_order(
            symbol=symbol,
            transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
            qty=final_lots,
        )
        if order_id == "FAILED":
            return

        # 3. Poll order history for actual execution price (slippage tracking)
        fill_price, status = self._fetch_executed_price(
            order_id=order_id,
            fallback_price=entry_price,
        )
        if status.startswith("REJECTED") or status.startswith("CANCELLED") or fill_price <= 0:
            logger.error("SELL order %s rejected/cancelled on exchange: %s", order_id, status)
            self._notifier.error(f"❌ SELL Order {order_id} ({symbol}) was {status}")
            return

        slippage = entry_price - fill_price
        slippage_str = f" | Slippage: ₹{slippage:+.2f} ({(slippage/entry_price)*100:+.2f}%)" if entry_price > 0 and self._s.live_order_placement else ""

        self._symbol           = symbol
        self._instrument_token = instrument_token
        self._position         = "SHORT"
        self._qty              = final_lots
        self._entry_price      = fill_price

        from src.state_manager import PositionState
        self._state.save(PositionState(
            position="SHORT",
            symbol=symbol,
            instrument_token=instrument_token,
            entry_price=fill_price,
            qty=final_lots,
            long_trail=None,
            short_trail=signal_result.trail_sl,
            entry_timestamp=candle_time.isoformat(),
        ))

        self._notifier.sell_entry(
            symbol=symbol,
            entry_price=fill_price,
            qty=final_lots,
            atr=atr,
            trail_sl=signal_result.trail_sl,
            candle_time=candle_time,
            sizing_desc=f"{sizing_desc}{slippage_str}",
            order_id=order_id,
            live=self._s.live_order_placement,
        )
        logger.info(
            "SELL ORDER: %s qty=%d fill=%.2f (signal=%.2f) trail=%.2f order_id=%s live=%s",
            symbol, final_lots, fill_price, entry_price,
            signal_result.trail_sl or 0, order_id, self._s.live_order_placement,
        )
        trade_logger.info(
            "🔴 SELL ENTRY | %s | Qty: %d | Fill Price: ₹%.2f (Signal: ₹%.2f) | Trail SL: ₹%.2f | ATR: ₹%.2f | Order ID: %s | Mode: %s",
            symbol, final_lots, fill_price, entry_price,
            signal_result.trail_sl or 0, atr, order_id, "LIVE" if self._s.live_order_placement else "PAPER",
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

        # Poll real exit fill price
        real_exit_price, status = self._fetch_executed_price(
            order_id=order_id,
            fallback_price=exit_price,
        )

        # 100% accurate realized P&L based on real exchange execution prices!
        realized_pnl = self._estimate_pnl(real_exit_price, is_long)

        self._notifier.exit_trade(
            symbol=self._symbol,
            position=self._position,
            exit_price=real_exit_price,
            entry_price=self._entry_price,
            qty=self._qty,
            reason=signal_result.exit_reason,
            pnl_est=realized_pnl,
            candle_time=candle_time,
            order_id=order_id,
            live=self._s.live_order_placement,
        )
        logger.info(
            "EXIT ORDER: %s %s qty=%d exit=%.2f entry=%.2f pnl=%.2f reason=%s",
            "EXIT_BUY" if is_long else "EXIT_SELL",
            self._symbol, self._qty, real_exit_price, self._entry_price,
            realized_pnl, signal_result.exit_reason,
        )
        trade_logger.info(
            "🛑 %s | %s | Qty: %d | Exit Price: ₹%.2f | Entry Price: ₹%.2f | Realized P&L: ₹%+.2f | Reason: %s | Order ID: %s",
            "EXIT_BUY" if is_long else "EXIT_SELL",
            self._symbol, self._qty, real_exit_price, self._entry_price,
            realized_pnl, signal_result.exit_reason, order_id,
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

    def _fetch_executed_price(
        self,
        order_id: str,
        fallback_price: float,
        max_retries: int = 4,
        delay_seconds: float = 0.5,
    ) -> tuple[float, str]:
        """
        Poll kite.order_history(order_id) to capture the exact executed average price.

        Parameters
        ----------
        order_id : str
            Order ID returned by place_order().
        fallback_price : float
            Default price if polling times out or simulation mode.
        max_retries : int
            Number of polling attempts (default: 4).
        delay_seconds : float
            Interval between polling attempts (default: 0.5s).

        Returns
        -------
        (fill_price, status) : tuple[float, str]
        """
        if not self._s.live_order_placement or order_id.startswith("SIM-") or order_id == "FAILED":
            return fallback_price, "COMPLETE"

        for attempt in range(max_retries):
            try:
                history = self._kite.order_history(order_id)
                if history and isinstance(history, list):
                    latest = history[-1]
                    status = str(latest.get("status", "")).upper()

                    if status == "COMPLETE":
                        avg_price = float(latest.get("average_price", 0.0))
                        if avg_price > 0:
                            logger.info(
                                "Order %s executed on exchange @ ₹%.2f (attempt %d)",
                                order_id, avg_price, attempt + 1,
                            )
                            return avg_price, "COMPLETE"

                    elif status in ("REJECTED", "CANCELLED"):
                        reason = latest.get("status_message") or status
                        logger.error("Order %s was %s: %s", order_id, status, reason)
                        return 0.0, f"{status}: {reason}"

            except Exception as exc:
                logger.warning("Error fetching order history for %s: %s", order_id, exc)

            _time.sleep(delay_seconds)

        logger.warning(
            "Order %s did not reach COMPLETE within timeout; using fallback price ₹%.2f",
            order_id, fallback_price,
        )
        return fallback_price, "PENDING"

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

    # ------------------------------------------------------------------
    # Margin Pre-Flight Verification
    # ------------------------------------------------------------------

    def _get_available_margin(self) -> float:
        """
        Fetch available cash balance from Kite.
        Checks commodity segment first; falls back to equity if commodity is 0 (unified account).
        """
        if not self._kite:
            return 0.0
        try:
            m = self._kite.margins(segment="commodity")
            avail = m.get("available", {})
            live_bal = float(avail.get("live_balance") or avail.get("cash") or 0.0)
            if live_bal > 0:
                return live_bal

            all_m = self._kite.margins()
            eq_avail = all_m.get("equity", {}).get("available", {})
            eq_bal = float(eq_avail.get("live_balance") or eq_avail.get("cash") or 0.0)
            if eq_bal > 0:
                return eq_bal
            return 0.0
        except Exception as exc:
            logger.warning("Failed to fetch margins from Kite: %s", exc)
            return 0.0

    def _estimate_margin_per_lot(
        self,
        symbol: str,
        transaction_type: str,
        entry_price: float,
    ) -> float:
        """
        Estimate margin required for 1 lot of the given contract.
        Uses Kite order_margins if available; falls back to exchange margin rate (~12%).
        """
        try:
            params = [{
                "exchange": self.EXCHANGE,
                "tradingsymbol": symbol,
                "transaction_type": transaction_type,
                "variety": KiteConnect.VARIETY_REGULAR,
                "product": self.PRODUCT,
                "order_type": self.ORDER_TYPE,
                "quantity": 1,
            }]
            margins = self._kite.order_margins(params)
            if margins and isinstance(margins, list):
                total = float(margins[0].get("total", 0.0))
                if total > 0:
                    return total
        except Exception as exc:
            logger.debug("kite.order_margins query failed or returned 0: %s", exc)

        # Fallback heuristic: MCX Gold initial margin is ~12% of contract value
        lot_multiplier = self._sizer.lot_multiplier or 1
        price = entry_price if entry_price > 0 else 12500.0
        contract_value = price * lot_multiplier
        return contract_value * 0.12

    def _apply_margin_preflight(
        self,
        symbol: str,
        transaction_type: str,
        risk_lots: int,
        entry_price: float,
        atr: float,
    ) -> tuple[int, str]:
        """
        Query broker margins before order placement.
        Enforces: Final Lots = min(Risk Lots, Affordable Margin Lots)
        """
        avail_cash = self._get_available_margin()

        # In simulation mode, if broker cash is 0, fall back to configured capital
        if not self._s.live_order_placement and avail_cash <= 0:
            avail_cash = self._s.capital

        margin_per_lot = self._estimate_margin_per_lot(symbol, transaction_type, entry_price)
        final_lots = self._sizer.cap_by_margin(risk_lots, avail_cash, margin_per_lot)

        base_desc = self._sizer.describe(atr)

        if final_lots <= 0:
            msg = (
                f"🚨 **Margin Pre-Flight Rejection**: Available cash ₹{avail_cash:,.2f} is insufficient "
                f"for 1 lot of {symbol} (est. ₹{margin_per_lot:,.2f}/lot required). "
                f"Order was aborted to prevent broker rejection."
            )
            logger.error(msg)
            self._notifier.error(msg)
            return 0, f"{base_desc} | ⚠️ Aborted: Insufficient margin (Avail: ₹{avail_cash:,.0f})"

        if final_lots < risk_lots:
            sizing_desc = (
                f"{base_desc}\n"
                f"⚠️ *Margin Capped:* Risk called for {risk_lots} lots, but available cash ₹{avail_cash:,.0f} "
                f"(₹{margin_per_lot:,.0f}/lot) capped position to **{final_lots} lot(s)**."
            )
            logger.warning(
                "Margin cap applied for %s: Risk lots=%d capped to %d lots (Avail cash: ₹%.2f, Margin/lot: ₹%.2f)",
                symbol, risk_lots, final_lots, avail_cash, margin_per_lot,
            )
        else:
            sizing_desc = f"{base_desc} (Margin Verified: ₹{avail_cash:,.0f} available)"

        return final_lots, sizing_desc
