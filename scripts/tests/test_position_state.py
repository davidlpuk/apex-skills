#!/usr/bin/env python3
"""
Tests for position state management — concurrent modifications, merge patterns.
Tests the locking patterns used in apex-trailing-stop.py, apex-reconcile.py,
apex-partial-close.py to ensure no last-writer-wins overwrites.
"""
import json
import sys
import threading
import time
import pytest

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_utils import atomic_write, safe_read, locked_read_modify_write


def _save_positions_locked(path, updated_positions):
    """
    Replicate the merge pattern from apex-trailing-stop.py:
    Only update positions whose ticker appears in our updated set.
    Other positions from concurrent writes are preserved.
    """
    our_map = {p.get('t212_ticker'): p for p in updated_positions}

    def _merge(current):
        current = current or []
        merged = []
        seen = set()
        for p in current:
            t = p.get('t212_ticker')
            if t in our_map:
                merged.append(our_map[t])
            else:
                merged.append(p)
            seen.add(t)
        for p in updated_positions:
            if p.get('t212_ticker') not in seen:
                merged.append(p)
        return merged

    locked_read_modify_write(path, _merge, default=[])


class TestPositionMerge:
    def test_update_preserves_other_positions(self, positions_file):
        """Updating AAPL stop should not remove XOM."""
        updated_aapl = [
            {"t212_ticker": "AAPL_US_EQ", "name": "AAPL",
             "quantity": 10, "entry": 180.0, "stop": 175.0, "status": "protected"},
        ]
        _save_positions_locked(positions_file, updated_aapl)

        positions = safe_read(positions_file, [])
        tickers = {p['t212_ticker'] for p in positions}
        assert 'XOM_US_EQ' in tickers, "XOM should be preserved after AAPL update"
        assert 'AAPL_US_EQ' in tickers

    def test_updated_stop_reflects_new_value(self, positions_file):
        updated = [
            {"t212_ticker": "AAPL_US_EQ", "name": "AAPL",
             "quantity": 10, "entry": 180.0, "stop": 175.0, "status": "protected"},
        ]
        _save_positions_locked(positions_file, updated)

        positions = safe_read(positions_file, [])
        aapl = next(p for p in positions if p['t212_ticker'] == 'AAPL_US_EQ')
        assert aapl['stop'] == 175.0

    def test_new_position_appended(self, positions_file):
        new_pos = [
            {"t212_ticker": "MSFT_US_EQ", "name": "MSFT",
             "quantity": 3, "entry": 420.0, "stop": 400.0, "status": "entry_placed"},
        ]
        _save_positions_locked(positions_file, new_pos)

        positions = safe_read(positions_file, [])
        tickers = {p['t212_ticker'] for p in positions}
        assert 'MSFT_US_EQ' in tickers
        assert 'AAPL_US_EQ' in tickers
        assert 'XOM_US_EQ' in tickers

    def test_concurrent_updates_no_data_loss(self, positions_file):
        """
        Thread A updates AAPL stop, Thread B updates XOM stop simultaneously.
        Both updates must survive — no last-writer-wins.
        """
        errors = []

        def update_aapl():
            try:
                time.sleep(0.01)  # Small offset to encourage interleaving
                updated = [{"t212_ticker": "AAPL_US_EQ", "name": "AAPL",
                            "quantity": 10, "entry": 180.0, "stop": 176.0,
                            "status": "protected"}]
                _save_positions_locked(positions_file, updated)
            except Exception as e:
                errors.append(('AAPL', e))

        def update_xom():
            try:
                updated = [{"t212_ticker": "XOM_US_EQ", "name": "XOM",
                            "quantity": 5, "entry": 95.0, "stop": 89.0,
                            "status": "protected"}]
                _save_positions_locked(positions_file, updated)
            except Exception as e:
                errors.append(('XOM', e))

        t1 = threading.Thread(target=update_aapl)
        t2 = threading.Thread(target=update_xom)
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors, f"Errors during concurrent update: {errors}"

        positions = safe_read(positions_file, [])
        tickers = {p['t212_ticker'] for p in positions}
        assert 'AAPL_US_EQ' in tickers
        assert 'XOM_US_EQ' in tickers
        assert len(positions) == 2, f"Expected 2 positions, got {len(positions)}"

        aapl = next(p for p in positions if p['t212_ticker'] == 'AAPL_US_EQ')
        xom  = next(p for p in positions if p['t212_ticker'] == 'XOM_US_EQ')
        assert aapl['stop'] == 176.0, "AAPL stop should be updated"
        assert xom['stop'] == 89.0,   "XOM stop should be updated"

    def test_partial_close_quantity_update(self, positions_file):
        """Partial close should update quantity and stop, preserve other positions."""
        _ticker = 'AAPL_US_EQ'
        _new_qty = 5.0
        _new_stop = 177.0

        def _update_partial(positions):
            positions = positions or []
            for pos in positions:
                if pos.get('t212_ticker') == _ticker:
                    pos['quantity'] = _new_qty
                    pos['stop'] = _new_stop
                    break
            return positions

        locked_read_modify_write(positions_file, _update_partial, default=[])

        positions = safe_read(positions_file, [])
        aapl = next(p for p in positions if p['t212_ticker'] == 'AAPL_US_EQ')
        assert aapl['quantity'] == 5.0
        assert aapl['stop'] == 177.0

        xom = next(p for p in positions if p['t212_ticker'] == 'XOM_US_EQ')
        assert xom['quantity'] == 5  # Unchanged
