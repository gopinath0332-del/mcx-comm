"""
tests/test_state_manager.py
===========================
Unit tests for StateManager persistence and reconciliation in PAPER and LIVE modes.
"""

import os
from unittest.mock import MagicMock

import pytest

from src.state_manager import PositionState, StateManager


@pytest.fixture
def temp_state_file(tmp_path):
    return str(tmp_path / "state.json")


@pytest.fixture
def state_mgr(temp_state_file):
    return StateManager(state_path=temp_state_file)


@pytest.fixture
def mock_signal_engine():
    engine = MagicMock()
    engine.position = None
    engine.long_trail = None
    engine.short_trail = None
    return engine


@pytest.fixture
def mock_order_manager():
    om = MagicMock()
    om.open_symbol = None
    om.open_qty = 0
    om.position = None
    return om


def test_save_and_load(state_mgr):
    pos = PositionState(
        position="SHORT",
        symbol="GOLDPETAL26DECFUT",
        instrument_token=148129543,
        entry_price=14500.0,
        qty=2,
        long_trail=None,
        short_trail=12750.0,
        entry_timestamp="2026-11-23T09:00:00",
        entry_bar=10,
    )
    state_mgr.save(pos)
    loaded = state_mgr.load()

    assert loaded is not None
    assert loaded.position == "SHORT"
    assert loaded.symbol == "GOLDPETAL26DECFUT"
    assert loaded.qty == 2
    assert loaded.entry_price == 14500.0
    assert loaded.short_trail == 12750.0


def test_clear_removes_file(state_mgr):
    pos = PositionState(
        position="LONG",
        symbol="GOLDPETAL26DECFUT",
        instrument_token=148129543,
        entry_price=12000.0,
        qty=1,
        long_trail=11800.0,
        short_trail=None,
        entry_timestamp="2026-11-23T09:00:00",
    )
    state_mgr.save(pos)
    assert state_mgr.load() is not None

    state_mgr.clear()
    assert state_mgr.load() is None


def test_reconcile_paper_mode_with_saved_state(state_mgr, mock_signal_engine, mock_order_manager):
    """In paper mode, saved state must be restored without checking broker, and not cleared."""
    pos = PositionState(
        position="SHORT",
        symbol="GOLDPETAL26DECFUT",
        instrument_token=148129543,
        entry_price=14500.0,
        qty=2,
        long_trail=None,
        short_trail=12750.0,
        entry_timestamp="2026-11-23T09:00:00",
    )
    state_mgr.save(pos)

    # Reconcile in paper mode (live_mode=False) with mock kite returning empty
    mock_kite = MagicMock()
    mock_kite.positions.return_value = {"net": []}

    res = state_mgr.reconcile(
        kite=mock_kite,
        signal_engine=mock_signal_engine,
        order_manager=mock_order_manager,
        live_mode=False,
    )

    assert res["status"] == "restored"
    assert res["restored"] is True
    assert res["symbol"] == "GOLDPETAL26DECFUT"
    assert res["position"] == "SHORT"
    assert res["qty"] == 2

    # Verify signal_engine and order_manager were restored
    mock_signal_engine.restore_state.assert_called_once_with(
        position="SHORT",
        long_trail=None,
        short_trail=12750.0,
        entry_bar=0,
    )
    mock_order_manager.restore_state.assert_called_once_with(
        symbol="GOLDPETAL26DECFUT",
        qty=2,
        position="SHORT",
        entry_price=14500.0,
    )

    # State file must NOT have been cleared
    assert state_mgr.load() is not None


def test_reconcile_paper_mode_clean(state_mgr, mock_signal_engine, mock_order_manager):
    res = state_mgr.reconcile(
        kite=None,
        signal_engine=mock_signal_engine,
        order_manager=mock_order_manager,
        live_mode=False,
    )
    assert res["status"] == "clean"
    assert res["restored"] is False


def test_reconcile_live_mode_stale_cleared(state_mgr, mock_signal_engine, mock_order_manager):
    """In live mode, if state exists but broker has no position, clear stale state."""
    pos = PositionState(
        position="SHORT",
        symbol="GOLDPETAL26DECFUT",
        instrument_token=148129543,
        entry_price=14500.0,
        qty=2,
        long_trail=None,
        short_trail=12750.0,
        entry_timestamp="2026-11-23T09:00:00",
    )
    state_mgr.save(pos)

    mock_kite = MagicMock()
    mock_kite.positions.return_value = {"net": []}

    res = state_mgr.reconcile(
        kite=mock_kite,
        signal_engine=mock_signal_engine,
        order_manager=mock_order_manager,
        live_mode=True,
    )

    assert res["status"] == "stale_cleared"
    assert res["restored"] is False
    assert state_mgr.load() is None


def test_reconcile_live_mode_restores_when_broker_matches(state_mgr, mock_signal_engine, mock_order_manager):
    pos = PositionState(
        position="SHORT",
        symbol="GOLDPETAL26DECFUT",
        instrument_token=148129543,
        entry_price=14500.0,
        qty=2,
        long_trail=None,
        short_trail=12750.0,
        entry_timestamp="2026-11-23T09:00:00",
    )
    state_mgr.save(pos)

    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {"exchange": "MCX", "tradingsymbol": "GOLDPETAL26DECFUT", "quantity": -2}
        ]
    }

    res = state_mgr.reconcile(
        kite=mock_kite,
        signal_engine=mock_signal_engine,
        order_manager=mock_order_manager,
        live_mode=True,
    )

    assert res["status"] == "restored"
    assert res["restored"] is True
    mock_signal_engine.restore_state.assert_called_once()
    mock_order_manager.restore_state.assert_called_once()
