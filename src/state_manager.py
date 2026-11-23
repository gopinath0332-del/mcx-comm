"""
src/state_manager.py
====================
Persists open position state to a local JSON file so the engine can survive
restarts without losing track of open trades.

State file: data/state.json

On startup, SessionManager calls reconcile() which:
  1. Reads state.json
  2. Checks open positions from the broker (Kite)
  3. Restores SignalEngine state if a position is found
  4. Clears stale state if the broker shows no position
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "state.json"
)


@dataclass
class PositionState:
    """Represents a persisted open position."""
    position: str           # "LONG" | "SHORT"
    symbol: str             # e.g. "GOLDPETAL26DECFUT"
    instrument_token: int
    entry_price: float
    qty: int
    long_trail: float | None
    short_trail: float | None
    entry_timestamp: str    # ISO format
    entry_bar: int = 0


class StateManager:
    """
    Manages position state persistence to / from a JSON file.

    Parameters
    ----------
    state_path : str
        Path to the JSON state file (auto-created if absent).
    """

    def __init__(self, state_path: str = DEFAULT_STATE_PATH) -> None:
        self._path = state_path
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, state: PositionState) -> None:
        """Persist position state to JSON."""
        try:
            data = asdict(state)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("State saved: %s qty=%d", state.symbol, state.qty)
        except Exception as exc:
            logger.error("Failed to save state: %s", exc)

    def clear(self) -> None:
        """Remove persisted state (position closed)."""
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
            logger.info("State cleared.")
        except Exception as exc:
            logger.error("Failed to clear state: %s", exc)

    def load(self) -> PositionState | None:
        """Load persisted state from JSON. Returns None if no state file."""
        if not os.path.exists(self._path):
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return PositionState(**data)
        except Exception as exc:
            logger.warning("Failed to load state: %s", exc)
            return None

    def reconcile(
        self,
        kite,
        signal_engine,
        order_manager,
        live_mode: bool,
    ) -> dict[str, Any]:
        """
        Startup reconciliation: compare saved state with broker positions.

        In PAPER mode (live_mode=False):
          - Restores saved state from state.json if present.
          - Never queries broker positions and never clears state as stale.

        In LIVE mode (live_mode=True):
          - Compares saved state against open broker positions from Kite.
          - Restores state if verified with broker.
          - Clears stale state if broker shows no open position.
          - Warns if broker has untracked position.

        Returns a status dict describing what was found / restored.
        """
        saved = self.load()
        broker_positions = self._fetch_broker_positions(kite, live_mode)

        if not live_mode:
            if saved is not None:
                logger.info(
                    "Reconciliation (PAPER): restoring saved state for %s (position=%s qty=%d)",
                    saved.symbol, saved.position, saved.qty,
                )
                signal_engine.restore_state(
                    position=saved.position,
                    long_trail=saved.long_trail,
                    short_trail=saved.short_trail,
                    entry_bar=saved.entry_bar,
                )
                order_manager.restore_state(
                    symbol=saved.symbol,
                    qty=saved.qty,
                    position=saved.position,
                    entry_price=saved.entry_price,
                )
                return {
                    "status": "restored",
                    "restored": True,
                    "symbol": saved.symbol,
                    "position": saved.position,
                    "qty": saved.qty,
                }
            logger.info("Reconciliation (PAPER): no open positions (clean start).")
            return {"status": "clean", "restored": False}

        if saved is None and not broker_positions:
            logger.info("Reconciliation: no open positions (clean start).")
            return {"status": "clean", "restored": False}

        if saved is not None and not broker_positions:
            # State file exists but no broker position — stale state, clear it
            logger.warning(
                "Reconciliation: stale state found for %s but no broker position. Clearing.",
                saved.symbol,
            )
            self.clear()
            return {"status": "stale_cleared", "restored": False}

        if saved is None and broker_positions:
            # Broker has position but we have no state — log warning, don't trade
            syms = [p.get("tradingsymbol") for p in broker_positions]
            logger.warning(
                "Reconciliation: broker has position(s) %s but no local state. "
                "Engine will NOT track these automatically. Please close manually or "
                "restart with state populated.",
                syms,
            )
            return {"status": "untracked_broker_position", "symbols": syms, "restored": False}

        # Both saved state and broker position exist — restore
        logger.info(
            "Reconciliation: restoring state for %s (position=%s qty=%d)",
            saved.symbol, saved.position, saved.qty,
        )
        signal_engine.restore_state(
            position=saved.position,
            long_trail=saved.long_trail,
            short_trail=saved.short_trail,
            entry_bar=saved.entry_bar,
        )
        order_manager.restore_state(
            symbol=saved.symbol,
            qty=saved.qty,
            position=saved.position,
            entry_price=saved.entry_price,
        )
        return {
            "status": "restored",
            "restored": True,
            "symbol": saved.symbol,
            "position": saved.position,
            "qty": saved.qty,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_broker_positions(self, kite, live_mode: bool) -> list[dict]:
        """
        Fetch open MCX positions from Kite.
        Returns list of position dicts with net qty != 0.
        """
        if not live_mode or kite is None:
            return []
        try:
            positions = kite.positions()
            net_positions = positions.get("net", [])
            open_mcx = [
                p for p in net_positions
                if p.get("exchange") == "MCX"
                and abs(int(p.get("quantity", 0))) > 0
            ]
            return open_mcx
        except Exception as exc:
            logger.warning("Could not fetch broker positions for reconciliation: %s", exc)
            return []
