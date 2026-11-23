"""
scripts/sync_state.py
=====================
CLI utility to inspect, manually set, or clear the engine's persisted position state
in data/state.json.

Useful for:
  - Checking active tracked position without grepping logs
  - Manually synchronizing an existing trade into the engine
  - Clearing state after manual broker interventions

Usage:
  # Check current state:
  python scripts/sync_state.py --status

  # Manually seed an active SHORT trade:
  python scripts/sync_state.py --set --symbol GOLDPETAL26DECFUT --position SHORT --qty 2 --entry-price 14500 --trail 12750

  # Clear persisted state:
  python scripts/sync_state.py --clear
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.state_manager import PositionState, StateManager

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage mcx-comm position state (data/state.json)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="Display current persisted position state")
    group.add_argument("--clear", action="store_true", help="Clear persisted state (mark FLAT)")
    group.add_argument("--set", action="store_true", help="Manually set position state")

    parser.add_argument("--symbol", type=str, help="Contract trading symbol, e.g. GOLDPETAL26DECFUT")
    parser.add_argument("--position", choices=["LONG", "SHORT"], help="Position direction")
    parser.add_argument("--qty", type=int, default=1, help="Number of units/lots")
    parser.add_argument("--entry-price", type=float, help="Entry price in INR")
    parser.add_argument("--trail", type=float, default=None, help="Trailing stop price in INR (optional)")
    parser.add_argument("--token", type=int, default=0, help="Instrument token (optional)")

    args = parser.parse_args()
    mgr = StateManager()

    if args.status:
        state = mgr.load()
        if state is None:
            print("No persisted state found. Engine position is FLAT.")
        else:
            print("=" * 50)
            print("Current Persisted Position State:")
            print(f"  Symbol:          {state.symbol}")
            print(f"  Position:        {state.position}")
            print(f"  Quantity:        {state.qty}")
            print(f"  Entry Price:     ₹{state.entry_price:,.2f}")
            print(f"  Long Trail:      {f'₹{state.long_trail:,.2f}' if state.long_trail else 'None'}")
            print(f"  Short Trail:     {f'₹{state.short_trail:,.2f}' if state.short_trail else 'None'}")
            print(f"  Entry Timestamp: {state.entry_timestamp}")
            print(f"  Entry Bar:       {state.entry_bar}")
            print("=" * 50)

    elif args.clear:
        mgr.clear()
        print("Persisted state cleared. Engine will start FLAT.")

    elif args.set:
        if not args.symbol or not args.position or not args.entry_price:
            print("Error: --set requires --symbol, --position, and --entry-price")
            sys.exit(1)

        long_trail = args.trail if args.position == "LONG" else None
        short_trail = args.trail if args.position == "SHORT" else None

        new_state = PositionState(
            position=args.position,
            symbol=args.symbol,
            instrument_token=args.token,
            entry_price=args.entry_price,
            qty=args.qty,
            long_trail=long_trail,
            short_trail=short_trail,
            entry_timestamp=datetime.now(tz=IST).isoformat(),
            entry_bar=0,
        )
        mgr.save(new_state)
        print(f"State saved successfully for {args.symbol} ({args.position} x {args.qty} @ ₹{args.entry_price:,.2f})")


if __name__ == "__main__":
    main()
