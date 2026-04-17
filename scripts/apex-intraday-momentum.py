#!/usr/bin/env python3
"""
apex-intraday-momentum.py
Intraday momentum analysis for all open positions.

For each position: fetches 5-day 15-minute data from yfinance, calculates
RSI(14), VWAP deviation, volume trend, distance to targets, and an overall
momentum verdict (STRONG / NEUTRAL / FADING / EXHAUSTED).

Output: apex-intraday-momentum.json
Safety: external-fetch (calls yfinance, writes to logs/)

Usage:
    python3 apex-intraday-momentum.py
"""
import json
import logging
import math
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_warning(m): print(f'WARNING: {m}')

LOG_DIR        = '/home/ubuntu/.picoclaw/logs'
POSITIONS_FILE = f'{LOG_DIR}/apex-positions.json'
TICKER_MAP     = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'
OUTPUT_FILE    = f'{LOG_DIR}/apex-intraday-momentum.json'
LOG_FILE       = f'{LOG_DIR}/apex-intraday-momentum.log'

logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE)],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)


def _rsi(closes, period=14):
    """Calculate RSI from a Series of close prices."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    last_gain = avg_gain.iloc[-1]
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def _volume_trend(volumes, window=6):
    """Compare recent volume to average. Returns ratio (>1 = above avg)."""
    if len(volumes) < window + 3:
        return None
    avg = volumes.iloc[:-3].tail(window).mean()
    recent = volumes.iloc[-3:].mean()
    if avg == 0:
        return None
    return round(recent / avg, 2)


def _build_yahoo_map(positions):
    """Map t212_ticker → yahoo ticker using apex-ticker-map.json."""
    tmap = safe_read(TICKER_MAP, {})
    # ticker map is keyed by yahoo ticker, values have 't212' field
    t212_to_yahoo = {}
    for yahoo, info in tmap.items():
        if isinstance(info, dict):
            t212_to_yahoo[info.get('t212', '')] = yahoo
    return t212_to_yahoo


def analyse_position(pos, yahoo_ticker):
    """Analyse intraday momentum for a single position."""
    result = {
        'ticker': pos.get('t212_ticker', ''),
        'name': pos.get('name', ''),
        'yahoo_ticker': yahoo_ticker,
        'entry': pos.get('entry'),
        'stop': pos.get('stop'),
        'target1': pos.get('target1'),
        'target2': pos.get('target2'),
    }
    _skip = {**result, 'verdict': 'NO_DATA', 'reason': 'Insufficient data'}

    try:
        import yfinance as yf
    except ImportError:
        return {**_skip, 'reason': 'yfinance not available'}

    try:
        hist = yf.Ticker(yahoo_ticker).history(period='5d', interval='15m')
    except Exception as e:
        return {**_skip, 'reason': f'yfinance error: {e}'}

    if hist is None or hist.empty or len(hist) < 20:
        return {**_skip, 'reason': 'Insufficient bars'}

    try:
        current_price = float(hist['Close'].iloc[-1])
        session_high = float(hist['High'].iloc[-20:].max())
        session_low = float(hist['Low'].iloc[-20:].min())

        if not math.isfinite(current_price):
            return {**_skip, 'reason': 'NaN price'}

        # RSI on 15-min bars
        rsi = _rsi(hist['Close'])

        # VWAP
        typical = (hist['High'] + hist['Low'] + hist['Close']) / 3
        cumvol = hist['Volume'].cumsum()
        vwap = None
        vwap_dev_pct = None
        if cumvol.iloc[-1] > 0:
            vwap_series = (typical * hist['Volume']).cumsum() / cumvol
            vwap = round(float(vwap_series.iloc[-1]), 4)
            if vwap > 0:
                vwap_dev_pct = round((current_price - vwap) / vwap * 100, 2)

        # Volume trend
        vol_trend = _volume_trend(hist['Volume'])

        # Distance from session high
        from_high_pct = round((current_price - session_high) / session_high * 100, 2)

        # MFE from entry
        entry = pos.get('entry', 0)
        mfe_from_entry = None
        if entry and entry > 0:
            mfe_from_entry = round((session_high - entry) / entry * 100, 2)

        # Distance to targets
        t1 = pos.get('target1')
        t2 = pos.get('target2')
        stop = pos.get('stop')
        dist_to_t1_pct = round((t1 - current_price) / current_price * 100, 2) if t1 else None
        dist_to_t2_pct = round((t2 - current_price) / current_price * 100, 2) if t2 else None
        dist_to_stop_pct = round((current_price - stop) / current_price * 100, 2) if stop else None

        # R-multiple (current)
        r_current = None
        if entry and stop and entry != stop:
            risk = abs(entry - stop)
            r_current = round((current_price - entry) / risk, 2)

        # Momentum verdict
        verdict = 'NEUTRAL'
        reasons = []

        if rsi is not None:
            if rsi >= 75:
                reasons.append(f'RSI overbought ({rsi})')
                verdict = 'EXHAUSTED'
            elif rsi >= 65:
                reasons.append(f'RSI elevated ({rsi})')
                if verdict != 'EXHAUSTED':
                    verdict = 'FADING'
            elif rsi <= 30:
                reasons.append(f'RSI oversold ({rsi})')
                verdict = 'FADING'

        if from_high_pct is not None and from_high_pct < -2.0:
            reasons.append(f'Reversed {from_high_pct:.1f}% from session high')
            if verdict == 'NEUTRAL':
                verdict = 'FADING'
            elif verdict == 'FADING':
                verdict = 'EXHAUSTED'

        if vol_trend is not None and vol_trend < 0.5:
            reasons.append(f'Volume fading ({vol_trend:.1f}x avg)')
            if verdict == 'NEUTRAL':
                verdict = 'FADING'

        if vol_trend is not None and vol_trend > 1.5 and from_high_pct is not None and from_high_pct > -0.5:
            if rsi is not None and rsi < 65:
                reasons.append(f'Strong volume ({vol_trend:.1f}x avg) near highs')
                verdict = 'STRONG'

        if t1 and current_price >= t1:
            reasons.append('At or above T1')
            if verdict in ('NEUTRAL', 'STRONG'):
                verdict = 'FADING'

        if not reasons:
            reasons.append('No strong directional signal')

        result.update({
            'current_price': current_price,
            'session_high': session_high,
            'session_low': session_low,
            'from_high_pct': from_high_pct,
            'mfe_from_entry_pct': mfe_from_entry,
            'rsi_15m': rsi,
            'vwap': vwap,
            'vwap_deviation_pct': vwap_dev_pct,
            'volume_trend': vol_trend,
            'dist_to_t1_pct': dist_to_t1_pct,
            'dist_to_t2_pct': dist_to_t2_pct,
            'dist_to_stop_pct': dist_to_stop_pct,
            'r_current': r_current,
            'verdict': verdict,
            'reasons': reasons,
        })

    except Exception as e:
        log.error(f"Error analysing {yahoo_ticker}: {e}")
        return {**_skip, 'reason': f'Analysis error: {e}'}

    return result


def run():
    positions = safe_read(POSITIONS_FILE, [])
    if not isinstance(positions, list):
        positions = []

    active = [p for p in positions if p.get('status') in ('protected', 'entry_placed')]
    if not active:
        log.info("No active positions to analyse")
        output = {
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
            'positions': [],
            'summary': 'No active positions',
        }
        atomic_write(OUTPUT_FILE, output)
        return output

    yahoo_map = _build_yahoo_map(active)
    results = []
    alerts = []

    for pos in active:
        t212 = pos.get('t212_ticker', '')
        yahoo = yahoo_map.get(t212)
        if not yahoo:
            # Fallback: try stripping _US_EQ / l_EQ suffix
            base = t212.replace('_US_EQ', '').replace('l_EQ', '')
            yahoo = base
            log.warning(f"No ticker map entry for {t212}, trying {yahoo}")

        analysis = analyse_position(pos, yahoo)
        results.append(analysis)

        if analysis.get('verdict') in ('FADING', 'EXHAUSTED'):
            alerts.append(analysis)

        log.info(f"{t212}: verdict={analysis.get('verdict')} "
                 f"rsi={analysis.get('rsi_15m')} "
                 f"from_high={analysis.get('from_high_pct')}%")

    output = {
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'positions': results,
        'alert_count': len(alerts),
        'summary': (
            f"{len(results)} positions analysed. "
            f"{len(alerts)} showing fading/exhausted momentum."
        ),
    }

    atomic_write(OUTPUT_FILE, output)
    log.info(f"Wrote {len(results)} position analyses to {OUTPUT_FILE}")
    return output


if __name__ == '__main__':
    result = run()
    print(json.dumps(result, indent=2))
