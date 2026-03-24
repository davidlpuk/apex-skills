#!/usr/bin/env python3
"""
Tests for apex_utils.py — atomic_write, safe_read, locked_read_modify_write.
"""
import json
import os
import sys
import threading
import time
import pytest

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_utils import atomic_write, safe_read, locked_read_modify_write


class TestAtomicWrite:
    def test_writes_valid_json(self, tmp_path):
        path = str(tmp_path / 'out.json')
        data = {'key': 'value', 'num': 42}
        result = atomic_write(path, data)
        assert result is True
        with open(path) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_overwrites_existing(self, tmp_path):
        path = str(tmp_path / 'out.json')
        atomic_write(path, {'v': 1})
        atomic_write(path, {'v': 2})
        assert safe_read(path)['v'] == 2

    def test_writes_list(self, tmp_path):
        path = str(tmp_path / 'list.json')
        atomic_write(path, [1, 2, 3])
        assert safe_read(path) == [1, 2, 3]

    def test_no_partial_file_on_error(self, tmp_path):
        """Atomic write should not leave a temp file on success."""
        path = str(tmp_path / 'out.json')
        atomic_write(path, {'x': 1})
        tmp_files = [f for f in os.listdir(tmp_path) if f.endswith('.tmp')]
        assert len(tmp_files) == 0

    def test_returns_false_on_bad_path(self):
        result = atomic_write('/nonexistent_dir/file.json', {})
        assert result is False


class TestSafeRead:
    def test_reads_valid_file(self, tmp_json):
        path = tmp_json({'hello': 'world'})
        assert safe_read(path) == {'hello': 'world'}

    def test_returns_default_for_missing_file(self, tmp_path):
        path = str(tmp_path / 'missing.json')
        assert safe_read(path, default=[]) == []

    def test_returns_empty_dict_for_missing_by_default(self, tmp_path):
        path = str(tmp_path / 'missing.json')
        assert safe_read(path) == {}

    def test_returns_default_for_corrupt_json(self, tmp_path):
        path = str(tmp_path / 'corrupt.json')
        with open(path, 'w') as f:
            f.write('{this is not json}')
        result = safe_read(path, default={'fallback': True})
        assert result == {'fallback': True}

    def test_reads_list(self, tmp_json):
        path = tmp_json([1, 2, 3])
        assert safe_read(path) == [1, 2, 3]


class TestLockedReadModifyWrite:
    def test_basic_modify(self, tmp_path):
        path = str(tmp_path / 'data.json')
        atomic_write(path, {'counter': 0})

        locked_read_modify_write(
            path,
            lambda d: {**d, 'counter': d.get('counter', 0) + 1},
            default={}
        )
        assert safe_read(path)['counter'] == 1

    def test_creates_file_with_default(self, tmp_path):
        path = str(tmp_path / 'new.json')
        locked_read_modify_write(path, lambda d: [*d, 'item'], default=[])
        assert safe_read(path) == ['item']

    def test_concurrent_modifications_both_survive(self, tmp_path):
        """
        Two threads each append their own ticker to positions list.
        Both entries must survive — no last-writer-wins overwrite.
        """
        path = str(tmp_path / 'positions.json')
        atomic_write(path, [])

        errors = []

        def append_ticker(ticker):
            try:
                def _add(positions):
                    positions = positions or []
                    # Avoid duplicates (idempotent)
                    if not any(p.get('t212_ticker') == ticker for p in positions):
                        positions.append({'t212_ticker': ticker, 'qty': 1})
                    return positions
                locked_read_modify_write(path, _add, default=[])
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=append_ticker, args=('AAPL_US_EQ',))
        t2 = threading.Thread(target=append_ticker, args=('XOM_US_EQ',))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
        positions = safe_read(path, [])
        tickers = {p['t212_ticker'] for p in positions}
        assert 'AAPL_US_EQ' in tickers
        assert 'XOM_US_EQ' in tickers
        assert len(positions) == 2

    def test_modifier_exception_does_not_corrupt_file(self, tmp_path):
        path = str(tmp_path / 'data.json')
        atomic_write(path, {'value': 42})

        def bad_modifier(data):
            raise ValueError("Intentional failure")

        with pytest.raises(ValueError):
            locked_read_modify_write(path, bad_modifier, default={})

        # Original data must still be intact
        assert safe_read(path)['value'] == 42
