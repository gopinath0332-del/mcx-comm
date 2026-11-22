"""
src/lot_sizer.py
================
ATR-based position sizing for MCX Gold futures.

Formula (from delta-exchange-journal image.png):
  lots = floor( Capital × (Base_Risk% / 100) / (ATR × ATR_Multiplier × Lot_Multiplier) )
  lots = max(1, lots)

Parameters for GOLDPETAL:
  Capital          = ₹2,00,000  (fixed, from config)
  Base_Risk%       = 2%         (config)
  ATR              = current 1H ATR of the active GOLDPETAL futures contract (in ₹/gram)
  ATR_Multiplier   = 1.5        (from image.png — stop distance multiplier)
  Lot_Multiplier   = 1          (1 gram per GOLDPETAL lot)

The stop-distance in rupees = ATR × ATR_Multiplier × Lot_Multiplier
Risk amount in rupees       = Capital × Base_Risk%
Lots                        = floor(Risk_Amount / Stop_Distance_per_lot)
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# ATR multiplier used for stop-loss distance in lot sizing (from image.png)
# This is DIFFERENT from the trailing SL multiplier (2.0) in the strategy.
# image.png shows 1.5 as the "ATR Multiplier" in the lot calculator.
LOT_SIZE_ATR_MULTIPLIER = 1.5


class LotSizer:
    """
    Calculates the number of lots to trade based on ATR-driven risk sizing.

    Parameters
    ----------
    capital : float
        Fixed trading capital in INR (e.g. 200000).
    base_risk_pct : float
        Risk per trade as percentage of capital (e.g. 2.0 for 2%).
    lot_multiplier : int
        Units per lot (e.g. 1 for GOLDPETAL, 10 for CRUDEOIL Mini).
    atr_size_multiplier : float
        ATR multiplier for stop-distance in the lot sizing formula (1.5 from image.png).
    max_lots : int
        Hard cap on maximum lots to guard against extreme low-ATR values.
    """

    def __init__(
        self,
        capital: float = 200_000.0,
        base_risk_pct: float = 2.0,
        lot_multiplier: int = 1,
        atr_size_multiplier: float = LOT_SIZE_ATR_MULTIPLIER,
        max_lots: int | None = None,
    ) -> None:
        self.capital            = float(capital)
        self.base_risk_pct      = float(base_risk_pct)
        self.lot_multiplier     = int(lot_multiplier)
        self.atr_size_multiplier = float(atr_size_multiplier)
        self.max_lots           = int(max_lots) if max_lots is not None and max_lots > 0 else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(self, atr: float) -> int:
        """
        Calculate the number of lots for a new entry.

        Parameters
        ----------
        atr : float
            Current ATR value in ₹ per unit (e.g. ₹35 per gram for GOLDPETAL).

        Returns
        -------
        int
            Number of lots (minimum 1, capped at max_lots if configured).
        """
        if atr <= 0:
            logger.warning("ATR is %.4f (≤ 0); defaulting to 1 lot.", atr)
            return 1

        risk_amount   = self.capital * (self.base_risk_pct / 100.0)
        stop_distance = atr * self.atr_size_multiplier * self.lot_multiplier

        if stop_distance <= 0:
            logger.warning("Stop distance is %.4f (≤ 0); defaulting to 1 lot.", stop_distance)
            return 1

        lots = math.floor(risk_amount / stop_distance)
        lots = max(1, lots)
        if self.max_lots is not None:
            lots = min(lots, self.max_lots)

        logger.info(
            "LotSizer: Capital=₹%.0f | Risk=%.1f%% (₹%.0f) | ATR=%.2f | "
            "StopDist=%.2f | Lots=%d",
            self.capital, self.base_risk_pct, risk_amount,
            atr, stop_distance, lots,
        )
        return lots

    def cap_by_margin(
        self,
        risk_lots: int,
        available_margin: float,
        margin_per_lot: float,
    ) -> int:
        """
        Cap risk_lots to affordable lots based on available broker margin.

        Formula:
          affordable_lots = floor(available_margin / margin_per_lot)
          final_lots      = min(risk_lots, affordable_lots)

        Returns 0 if available margin cannot cover even 1 lot.
        """
        if margin_per_lot <= 0:
            return risk_lots
        if available_margin <= 0:
            return 0
        affordable_lots = math.floor(available_margin / margin_per_lot)
        if affordable_lots <= 0:
            return 0
        return min(risk_lots, affordable_lots)

    def describe(self, atr: float) -> str:
        """Human-readable sizing breakdown for Discord messages."""
        lots = self.calculate(atr)
        risk_amount   = self.capital * (self.base_risk_pct / 100.0)
        stop_distance = atr * self.atr_size_multiplier * self.lot_multiplier
        return (
            f"Capital ₹{self.capital:,.0f} | Risk {self.base_risk_pct}% (₹{risk_amount:,.0f}) | "
            f"ATR ₹{atr:.2f} × {self.atr_size_multiplier} × {self.lot_multiplier} = "
            f"₹{stop_distance:.2f}/lot → **{lots} lot(s)**"
        )
