"""
src/logger.py
=============
Enterprise logging mechanism for the MCX Gold commodity trading engine.

Log Destinations:
  1. Console (stdout)   : Clean output formatted for console viewing.
  2. logs/engine.log    : Comprehensive runtime log (includes DEBUG detail, rotated daily, 30-day retention).
  3. logs/trades.log    : Clean, dedicated trade audit ledger (orders, entries, exits, PnL).
  4. logs/error.log     : Dedicated warning & error log for fast troubleshooting.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

# Always resolve logs relative to the project root directory
DEFAULT_LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "logs")
)


def get_trade_logger() -> logging.Logger:
    """Return the dedicated trade audit logger (writes to logs/trades.log)."""
    return logging.getLogger("mcx.trades")


def setup_logging(
    log_level: str = "INFO",
    log_dir: str = DEFAULT_LOG_DIR,
    retention_days: int = 30,
    file_debug: bool = True,
) -> None:
    """
    Configure enterprise-grade logging with multi-destination routing.

    Parameters
    ----------
    log_level : str
        Console log level ("DEBUG", "INFO", "WARNING", "ERROR").
    log_dir : str
        Directory path where log files will be written.
    retention_days : int
        Number of daily rotated log files to retain.
    file_debug : bool
        Whether engine.log should record DEBUG-level details regardless of console level.
    """
    os.makedirs(log_dir, exist_ok=True)

    console_level = getattr(logging, log_level.upper(), logging.INFO)
    file_level = logging.DEBUG if file_debug else console_level

    # Formatting
    detailed_fmt = "%(asctime)s [%(levelname)-7s] [%(name)s:%(lineno)d] — %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    detailed_formatter = logging.Formatter(detailed_fmt, datefmt=date_fmt)

    console_fmt = "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s"
    console_formatter = logging.Formatter(console_fmt, datefmt=date_fmt)

    root = logging.getLogger()
    root.setLevel(min(console_level, file_level))
    root.handlers.clear()

    # 1. Console Handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    # 2. Main Engine Log (logs/engine.log - daily rotation)
    engine_log_file = os.path.join(log_dir, "engine.log")
    engine_handler = logging.handlers.TimedRotatingFileHandler(
        engine_log_file, when="midnight", backupCount=retention_days, encoding="utf-8"
    )
    engine_handler.setLevel(file_level)
    engine_handler.setFormatter(detailed_formatter)
    root.addHandler(engine_handler)

    # 3. Dedicated Error Log (logs/error.log - daily rotation)
    error_log_file = os.path.join(log_dir, "error.log")
    error_handler = logging.handlers.TimedRotatingFileHandler(
        error_log_file, when="midnight", backupCount=retention_days, encoding="utf-8"
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(detailed_formatter)
    root.addHandler(error_handler)

    # 4. Dedicated Trade Audit Logger (logs/trades.log)
    trade_logger = logging.getLogger("mcx.trades")
    trade_logger.setLevel(logging.INFO)
    trade_logger.propagate = True  # Also appears in engine.log

    trade_log_file = os.path.join(log_dir, "trades.log")
    trade_handler = logging.handlers.TimedRotatingFileHandler(
        trade_log_file, when="midnight", backupCount=retention_days, encoding="utf-8"
    )
    trade_fmt = "%(asctime)s [%(levelname)-7s] %(message)s"
    trade_handler.setFormatter(logging.Formatter(trade_fmt, datefmt=date_fmt))
    trade_handler.setLevel(logging.INFO)
    trade_logger.handlers.clear()
    trade_logger.addHandler(trade_handler)

    # Suppress noisy external library logs
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
