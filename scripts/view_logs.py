"""
scripts/view_logs.py
====================
Interactive CLI utility to view, filter, and live-tail engine logs.

Usage:
  python scripts/view_logs.py                # View last 50 lines of engine.log
  python scripts/view_logs.py -n 100         # View last 100 lines
  python scripts/view_logs.py --trades       # View trade audit log (trades.log)
  python scripts/view_logs.py --errors       # View error & warning log (error.log)
  python scripts/view_logs.py --follow       # Live follow/tail logs (like tail -f)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "logs")
)


def tail_file(filepath: str, n_lines: int = 50) -> list[str]:
    """Read the last n lines of a file."""
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        return lines[-n_lines:]


def follow_file(filepath: str) -> None:
    """Stream new lines appended to a file in real-time."""
    if not os.path.exists(filepath):
        print(f"Log file not found: {filepath}. Waiting for creation...")
        while not os.path.exists(filepath):
            time.sleep(1)

    print(f"--- Tailing {os.path.basename(filepath)} (Press Ctrl+C to exit) ---")
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        # Move cursor to end of file
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                sys.stdout.write(colorize(line))
                sys.stdout.flush()
            else:
                time.sleep(0.5)


def colorize(text: str) -> str:
    """Add basic ANSI terminal colors if supported."""
    if "ERROR" in text or "CRITICAL" in text or "🔴" in text:
        return f"\033[91m{text}\033[0m"
    if "WARNING" in text or "⚠️" in text or "🟡" in text:
        return f"\033[93m{text}\033[0m"
    if "BUY" in text or "🟢" in text or "✅" in text:
        return f"\033[92m{text}\033[0m"
    if "📊" in text or "📈" in text:
        return f"\033[96m{text}\033[0m"
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="MCX Gold Engine Log Viewer")
    parser.add_argument(
        "-n", "--lines", type=int, default=50, help="Number of lines to show (default: 50)"
    )
    parser.add_argument(
        "-t", "--trades", action="store_true", help="View trade ledger (logs/trades.log)"
    )
    parser.add_argument(
        "-e", "--errors", action="store_true", help="View error & warning log (logs/error.log)"
    )
    parser.add_argument(
        "-f", "--follow", action="store_true", help="Follow log in real time (live tail)"
    )

    args = parser.parse_args()

    if args.trades:
        target_file = os.path.join(LOG_DIR, "trades.log")
        title = "Trade Ledger (logs/trades.log)"
    elif args.errors:
        target_file = os.path.join(LOG_DIR, "error.log")
        title = "Error & Warning Log (logs/error.log)"
    else:
        target_file = os.path.join(LOG_DIR, "engine.log")
        title = "Main Engine Log (logs/engine.log)"

    if args.follow:
        try:
            follow_file(target_file)
        except KeyboardInterrupt:
            print("\nStopped log tail.")
        return

    print("=" * 70)
    print(f"📖 {title}")
    print("=" * 70)

    if not os.path.exists(target_file):
        print(f"ℹ️  No log file found at {target_file} yet.")
        print("   Logs will be created automatically once the engine runs.")
        return

    lines = tail_file(target_file, n_lines=args.lines)
    if not lines:
        print("ℹ️  Log file is currently empty.")
        return

    for line in lines:
        sys.stdout.write(colorize(line))

    print("=" * 70)
    print(f"Showing last {len(lines)} lines from {os.path.basename(target_file)}")


if __name__ == "__main__":
    main()
