#!/usr/bin/env python3
"""
Pytest fixtures for Apex trading system tests.
"""
import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')


@pytest.fixture
def tmp_json(tmp_path):
    """Return a factory: tmp_json(data) creates a temp JSON file and returns its path."""
    def _make(data, name='test.json'):
        path = str(tmp_path / name)
        with open(path, 'w') as f:
            json.dump(data, f)
        return path
    return _make


@pytest.fixture
def positions_file(tmp_path):
    """Temp positions JSON file pre-loaded with two positions."""
    path = str(tmp_path / 'apex-positions.json')
    data = [
        {"t212_ticker": "AAPL_US_EQ", "name": "AAPL", "quantity": 10,
         "entry": 180.0, "stop": 170.0, "status": "protected"},
        {"t212_ticker": "XOM_US_EQ",  "name": "XOM",  "quantity": 5,
         "entry": 95.0,  "stop": 88.0,  "status": "protected"},
    ]
    with open(path, 'w') as f:
        json.dump(data, f)
    return path


@pytest.fixture
def minimal_intel():
    """Minimal intelligence dict that passes all filter gates."""
    sectors = ('Technology', 'Energy', 'Financials', 'Healthcare', 'Consumer')
    return {
        'vix':                      18.0,
        'regime_status':            'OK',
        'direction_status':         'OK',
        'direction_blocks':         [],
        'geo_status':               'CLEAR',
        'geo':                      {'overall': 'CLEAR'},
        'breadth':                  65,
        'sector_scores':            {s: 5 for s in sectors},
        'sector_breadth':           {s: {'breadth_200': 55, 'health': 'NEUTRAL'}
                                     for s in sectors},
        'leading_sectors':          ['Technology'],
        'lagging_sectors':          [],
        'earnings_blocked':         set(),
        'news_blocked':             set(),
        'size_multiplier':          1.0,
        'drawdown_status':          'NORMAL',
        'drawdown_pct':             0.0,
        'open_positions':           [],
        'position_vix_sensitivity': {},
    }


@pytest.fixture
def trend_signal():
    """Minimal TREND signal."""
    return {
        'name':        'AAPL',
        'ticker':      'AAPL',
        't212_ticker': 'AAPL_US_EQ',
        'signal_type': 'TREND',
        'total_score': 8,
        'entry':       180.0,
        'stop':        170.0,
    }


@pytest.fixture
def contrarian_signal():
    """Minimal CONTRARIAN signal."""
    return {
        'name':             'AAPL',
        'ticker':           'AAPL',
        't212_ticker':      'AAPL_US_EQ',
        'signal_type':      'CONTRARIAN',
        'contrarian_score': 7,
        'total_score':      7,
        'entry':            150.0,
        'stop':             138.0,
    }
