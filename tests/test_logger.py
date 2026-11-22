"""
tests/test_logger.py
====================
Unit tests for the enterprise multi-destination logging setup.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import pytest

from src.logger import get_trade_logger, setup_logging


@pytest.fixture
def temp_log_dir():
    dirpath = tempfile.mkdtemp()
    yield dirpath
    shutil.rmtree(dirpath, ignore_errors=True)


def test_setup_logging_creates_destinations(temp_log_dir):
    setup_logging(log_level="INFO", log_dir=temp_log_dir, file_debug=True)

    root = logging.getLogger()
    engine_logger = logging.getLogger("src.session_manager")
    trade_logger = get_trade_logger()

    # Log messages across levels
    engine_logger.debug("Debug test message")
    engine_logger.info("Info test message")
    engine_logger.warning("Warning test message")
    engine_logger.error("Error test message")
    trade_logger.info("BUY ENTRY: Test Trade")

    # Flush all handlers
    for h in root.handlers:
        h.flush()
    for h in trade_logger.handlers:
        h.flush()

    engine_log = os.path.join(temp_log_dir, "engine.log")
    error_log = os.path.join(temp_log_dir, "error.log")
    trades_log = os.path.join(temp_log_dir, "trades.log")

    assert os.path.exists(engine_log)
    assert os.path.exists(error_log)
    assert os.path.exists(trades_log)

    with open(engine_log, "r", encoding="utf-8") as f:
        content = f.read()
        assert "Debug test message" in content
        assert "Info test message" in content
        assert "Warning test message" in content
        assert "Error test message" in content
        assert "BUY ENTRY: Test Trade" in content

    with open(error_log, "r", encoding="utf-8") as f:
        error_content = f.read()
        assert "Warning test message" in error_content
        assert "Error test message" in error_content
        assert "Debug test message" not in error_content
        assert "Info test message" not in error_content

    with open(trades_log, "r", encoding="utf-8") as f:
        trades_content = f.read()
        assert "BUY ENTRY: Test Trade" in trades_content
        assert "Debug test message" not in trades_content
