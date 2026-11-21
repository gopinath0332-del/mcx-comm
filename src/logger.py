"""
src/logger.py
=============
Configures rotating file + console logging for the MCX engine.

Log files are written to logs/ directory with daily rotation.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    """
    Set up root logger with:
      - Console handler (stdout, coloured where supported)
      - Rotating file handler → logs/engine.log (daily rotation, 14-day retention)
    """
    os.makedirs(log_dir, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)

    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Daily rotating file handler
    log_file = os.path.join(log_dir, "engine.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
