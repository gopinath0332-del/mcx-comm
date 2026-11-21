"""
src/contract_selector.py
=========================
Selects the correct MCX GOLDPETAL futures contract for trading.

Rules (from REQUIREMENT.md):
  1. If a position is already open in the exchange for Gold, use that
     month's contract to track exit signals.
  2. Otherwise, select the NEXT month's expiring future.

MCX GOLDPETAL futures symbol format on Kite:
  GOLDPETAL<YY><MMMFUT>  e.g. GOLDPETAL26DECFUT, GOLDPETAL27JANFUT
"""

from __future__ import annotations

import logging
from datetime import date, datetime

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

logger = logging.getLogger(__name__)

# Months abbreviation map (matches Kite symbol format)
_MCX_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


class ContractSelector:
    """
    Resolves which MCX GOLDPETAL futures contract to use for a given session.

    Parameters
    ----------
    instrument_prefix : str
        Base name without exchange prefix, e.g. "GOLDPETAL".
    exchange : str
        Exchange string, e.g. "MCX".
    """

    def __init__(
        self,
        instrument_prefix: str = "GOLDPETAL",
        exchange: str = "MCX",
    ) -> None:
        self.instrument_prefix = instrument_prefix.replace("MCX:", "").strip()
        self.exchange = exchange
        self._instruments_cache: list[dict] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_active_contract(
        self,
        kite,
        open_position_symbol: str | None = None,
    ) -> tuple[str, int]:
        """
        Determine the contract to trade or track.

        Parameters
        ----------
        kite : KiteConnect
            Authenticated Kite instance.
        open_position_symbol : str | None
            Symbol of a currently open position (from StateManager), if any.
            When provided, that contract is returned directly (without selection logic),
            so exit signals are tracked on the correct expiry month.

        Returns
        -------
        (trading_symbol, instrument_token) : tuple[str, int]
            e.g. ("GOLDPETAL26DECFUT", 148129543)
        """
        # Rule 1: If a position is open, stick to that month's contract
        if open_position_symbol:
            token = self._resolve_token_for_symbol(kite, open_position_symbol)
            logger.info(
                "Open position detected: using existing contract %s (token=%s)",
                open_position_symbol, token,
            )
            return open_position_symbol, token

        # Rule 2: Select next-month contract
        instruments = self._fetch_goldpetal_instruments(kite)
        symbol, token = self._select_next_month(instruments)
        logger.info(
            "No open position: selected next-month contract %s (token=%d)",
            symbol, token,
        )
        return symbol, token

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_goldpetal_instruments(self, kite) -> list[dict]:
        """Fetch and cache MCX instruments, filtered to GOLDPETAL futures."""
        if self._instruments_cache is not None:
            return self._instruments_cache

        try:
            all_instruments = kite.instruments(self.exchange)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch MCX instruments from Kite: {exc}"
            ) from exc

        # Filter: instrument_type == FUT and name matches prefix
        futures = [
            inst for inst in all_instruments
            if inst.get("instrument_type") == "FUT"
            and inst.get("name", "").upper() == self.instrument_prefix.upper()
        ]

        if not futures:
            raise RuntimeError(
                f"No GOLDPETAL futures found in MCX instruments list. "
                f"Check that your Zerodha account has commodity segment enabled."
            )

        self._instruments_cache = futures
        logger.debug("Found %d GOLDPETAL futures contracts.", len(futures))
        return futures

    def _select_next_month(self, instruments: list[dict]) -> tuple[str, int]:
        """
        Select the nearest-expiry future whose expiry is AFTER today.
        This is the "next month" contract to trade fresh entries in.

        On expiry day itself, the next contract (the one after the expiring one)
        is selected to avoid trading into expiry.
        """
        today = datetime.now(tz=IST).date()

        # Collect futures with expiry > today, sort ascending by expiry
        future_contracts = []
        for inst in instruments:
            expiry = inst.get("expiry")
            if expiry is None:
                continue
            # expiry may be a date object or a string
            if isinstance(expiry, str):
                try:
                    expiry = datetime.strptime(expiry, "%Y-%m-%d").date()
                except ValueError:
                    continue
            elif hasattr(expiry, "date"):
                expiry = expiry.date()

            if expiry > today:
                future_contracts.append((expiry, inst))

        if not future_contracts:
            raise RuntimeError(
                "No GOLDPETAL futures with expiry after today found. "
                "Check MCX instruments list."
            )

        future_contracts.sort(key=lambda x: x[0])

        # Pick the nearest future (soonest expiry after today)
        _, selected = future_contracts[0]
        symbol = selected["tradingsymbol"]
        token = int(selected["instrument_token"])
        return symbol, token

    def _resolve_token_for_symbol(self, kite, symbol: str) -> int:
        """
        Look up the instrument token for a known trading symbol.
        Falls back to 0 if not found (safe fallback, historical API may still work).
        """
        try:
            instruments = self._fetch_goldpetal_instruments(kite)
            for inst in instruments:
                if inst.get("tradingsymbol", "").upper() == symbol.upper():
                    return int(inst["instrument_token"])
        except Exception as exc:
            logger.warning("Could not resolve token for symbol %s: %s", symbol, exc)
        return 0

    def clear_cache(self) -> None:
        """Force re-fetch of instruments on next call (e.g. after rollover)."""
        self._instruments_cache = None
