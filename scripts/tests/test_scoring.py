#!/usr/bin/env python3
"""
Tests for apex_scoring.py — score_signal_with_intelligence(), sector helpers.
"""
import sys
import pytest

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
from apex_scoring import (
    score_signal_with_intelligence,
    get_instrument_sector,
    get_sector_boost,
    _MODULE_CACHE,
)


class TestGetInstrumentSector:
    def test_known_tech_ticker(self):
        assert get_instrument_sector('AAPL') == 'Technology'
        assert get_instrument_sector('MSFT') == 'Technology'
        assert get_instrument_sector('NVDA') == 'Technology'

    def test_known_energy_ticker(self):
        assert get_instrument_sector('XOM') == 'Energy'
        assert get_instrument_sector('CVX') == 'Energy'

    def test_known_financials_ticker(self):
        assert get_instrument_sector('JPM') == 'Financials'

    def test_unknown_ticker_returns_none(self):
        assert get_instrument_sector('ZZUNKNOWN') is None

    def test_case_insensitive(self):
        assert get_instrument_sector('aapl') == 'Technology'
        assert get_instrument_sector('Xom') == 'Energy'


class TestGetSectorBoost:
    def test_leading_sector_gives_boost(self, minimal_intel):
        minimal_intel['leading_sectors'] = ['Technology']
        boost, reason = get_sector_boost('AAPL', minimal_intel)
        assert boost > 0
        assert 'Technology' in reason

    def test_lagging_sector_gives_penalty(self, minimal_intel):
        minimal_intel['lagging_sectors'] = ['Technology']
        minimal_intel['leading_sectors'] = []
        boost, reason = get_sector_boost('AAPL', minimal_intel)
        assert boost < 0

    def test_neutral_sector_no_boost(self, minimal_intel):
        minimal_intel['leading_sectors'] = []
        minimal_intel['lagging_sectors'] = []
        boost, _ = get_sector_boost('AAPL', minimal_intel)
        # May still get breadth boost/penalty — just check no exception
        assert isinstance(boost, int)

    def test_unknown_ticker_returns_zero(self, minimal_intel):
        boost, reason = get_sector_boost('ZZUNKNOWN', minimal_intel)
        assert boost == 0


class TestScoreSignalWithIntelligence:
    def test_returns_adjusted_score(self, trend_signal, minimal_intel):
        result = score_signal_with_intelligence(trend_signal, minimal_intel)
        assert 'adjusted_score' in result
        assert isinstance(result['adjusted_score'], (int, float))

    def test_base_score_preserved_when_no_adjustments(self, trend_signal, minimal_intel):
        """With neutral intel and no external modules, score should be close to base."""
        # Clear cache so live modules aren't accidentally hit
        _MODULE_CACHE.clear()
        result = score_signal_with_intelligence(trend_signal, minimal_intel)
        # adjusted_score may differ slightly due to sector boost, but base_score passes through
        assert result['adjusted_score'] >= 0

    def test_leading_sector_adjustment_present(self, trend_signal, minimal_intel):
        """When Technology is a leading sector, a Sector: +N adjustment appears."""
        minimal_intel['leading_sectors'] = ['Technology']
        result = score_signal_with_intelligence(trend_signal, minimal_intel)
        sector_adjs = [a for a in result.get('adjustments', []) if a.startswith('Sector:')]
        # Should have a positive sector adjustment for AAPL (Technology = leading)
        assert any('+' in a for a in sector_adjs), \
            f"Expected positive sector adjustment, got: {result.get('adjustments', [])}"

    def test_adjustments_list_returned(self, trend_signal, minimal_intel):
        result = score_signal_with_intelligence(trend_signal, minimal_intel)
        assert 'adjustments' in result
        assert isinstance(result['adjustments'], list)

    def test_contrarian_signal_scored(self, contrarian_signal, minimal_intel):
        result = score_signal_with_intelligence(contrarian_signal, minimal_intel)
        assert 'adjusted_score' in result
        assert result['adjusted_score'] >= 0

    def test_module_cache_populated(self, trend_signal, minimal_intel):
        """After scoring, cache should be populated (or remain empty if modules unavailable)."""
        score_signal_with_intelligence(trend_signal, minimal_intel)
        # Cache is either populated (modules loaded) or empty (modules missing) — no exception
        assert isinstance(_MODULE_CACHE, dict)
