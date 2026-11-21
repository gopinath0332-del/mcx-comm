"""
main.py
=======
MCX Gold Commodity Trading Engine — Entry Point.

Usage:
  python main.py

Environment:
  Set all credentials in .env (see .env.example).
  Strategy parameters are in config/settings.yml.
  The Zerodha access token is read automatically from the nifty-strike-engine project.
"""

from __future__ import annotations

import sys
import os

# Ensure the project root is on the path when running directly
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import get_settings
from src.logger import setup_logging
from src.session_manager import SessionManager


def main() -> None:
    settings = get_settings()
    setup_logging(log_level=settings.log_level)

    import logging
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("MCX Gold Engine — %s", settings.strategy_name)
    logger.info("Mode: %s", "LIVE" if settings.live_order_placement else "PAPER")
    logger.info("Capital: ₹%.0f | Risk: %.1f%%", settings.capital, settings.base_risk_pct)
    logger.info("Instrument: %s | Interval: %dH", settings.instrument_prefix, settings.candle_interval_minutes)
    logger.info("=" * 60)

    sm = SessionManager(settings)
    try:
        sm.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down…")
        sm.stop()
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sm.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
