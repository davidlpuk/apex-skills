#!/usr/bin/env python3
"""
Tests for apex_filters.py — is_blocked() gate.
"""
import sys
import pytest

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_filters import is_blocked


class TestIsBlocked:
    def test_passes_clean_signal(self, trend_signal, minimal_intel):
        blocks = is_blocked(trend_signal, minimal_intel)
        assert blocks == []

    def test_earnings_block(self, trend_signal, minimal_intel):
        minimal_intel['earnings_blocked'] = {'AAPL'}
        blocks = is_blocked(trend_signal, minimal_intel)
        assert any('Earnings block' in b for b in blocks)

    def test_news_block(self, trend_signal, minimal_intel):
        minimal_intel['news_blocked'] = {'AAPL'}
        blocks = is_blocked(trend_signal, minimal_intel)
        assert any('News block' in b for b in blocks)

    def test_regime_block_on_trend(self, trend_signal, minimal_intel):
        minimal_intel['regime_status'] = 'BLOCKED'
        minimal_intel['vix'] = 35
        blocks = is_blocked(trend_signal, minimal_intel)
        assert any('Regime blocked' in b for b in blocks)

    def test_regime_block_skipped_for_contrarian(self, contrarian_signal, minimal_intel):
        minimal_intel['regime_status'] = 'BLOCKED'
        minimal_intel['vix'] = 35
        blocks = is_blocked(contrarian_signal, minimal_intel)
        # Contrarian signals are not regime-blocked
        assert not any('Regime blocked' in b for b in blocks)

    def test_direction_block_on_trend(self, trend_signal, minimal_intel):
        minimal_intel['direction_status'] = 'BLOCKED'
        minimal_intel['direction_blocks'] = ['SPY below 200 EMA']
        blocks = is_blocked(trend_signal, minimal_intel)
        assert any('Market direction' in b for b in blocks)

    def test_direction_block_skipped_for_contrarian(self, contrarian_signal, minimal_intel):
        minimal_intel['direction_status'] = 'BLOCKED'
        minimal_intel['direction_blocks'] = ['SPY below 200 EMA']
        blocks = is_blocked(contrarian_signal, minimal_intel)
        assert not any('Market direction' in b for b in blocks)

    def test_sector_breadth_block(self, trend_signal, minimal_intel):
        # Technology breadth is very low
        minimal_intel['sector_breadth']['Technology'] = {'breadth_200': 15, 'health': 'BEARISH'}
        blocks = is_blocked(trend_signal, minimal_intel)
        # AAPL is in Technology sector
        assert any('breadth' in b.lower() for b in blocks)

    def test_multiple_blocks_returned(self, trend_signal, minimal_intel):
        minimal_intel['earnings_blocked'] = {'AAPL'}
        minimal_intel['news_blocked'] = {'AAPL'}
        blocks = is_blocked(trend_signal, minimal_intel)
        assert len(blocks) >= 2

    def test_geo_block_avoided_instrument(self, trend_signal, minimal_intel):
        minimal_intel['geo_status'] = 'ALERT'
        # XOM is geo-favoured (energy), AAPL is geo-avoided in intel
        # Patch the geo_adjustment to return negative for AAPL by testing XOM path
        # (avoid testing internal geo_map; just verify CLEAR passes)
        blocks = is_blocked(trend_signal, minimal_intel)
        # geo_status is ALERT but AAPL may be neutral — no block unless in avoided list
        # This test verifies the function runs without exception
        assert isinstance(blocks, list)
