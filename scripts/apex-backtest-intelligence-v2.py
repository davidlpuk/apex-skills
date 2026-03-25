#!/usr/bin/env python3
"""
Apex Backtest Intelligence V2
Enriched historical intelligence reconstruction.

Extends BacktestIntelligence with 4 historically-reconstructable layers:
  1. RS  (Relative Strength) — stock 1M/3M return vs SPY
  2. MTF (Multi-Timeframe)   — weekly EMA alignment
  3. FRED macro              — FRED series indexed by date
  4. Sentiment proxy         — VIX 5-day delta

Layers that remain at 0 (truly live-only):
  - EDGAR insider (no bulk historical download)
  - Options flow  (live chain data only)
"""
import math
import os
import json
import requests
import time
from bisect import bisect_right
from datetime import datetime, timezone

import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

# Import base class from existing backtest
try:
    from importlib import import_module
    _bt = import_module('apex-backtest')
    BacktestIntelligence = _bt.BacktestIntelligence
    calculate_ema = _bt.calculate_ema
    calculate_rsi = _bt.calculate_rsi
    vix_scale = _bt.vix_scale
    BACKTEST_UNIVERSE = _bt.BACKTEST_UNIVERSE
    SECTOR_ETFS = _bt.SECTOR_ETFS
except Exception:
    # Fallback: minimal stubs (shouldn't happen in production)
    class BacktestIntelligence:
        def __init__(self, *a, **kw): pass
        def get_intel(self, date_str): return {}
    def calculate_ema(c, p):
        if not c: return 0
        k = 2 / (p + 1)
        e = c[0]
        for p_ in c[1:]: e = p_ * k + e * (1 - k)
        return e
    def calculate_rsi(c, p=14):
        if len(c) < p + 1: return 50
        g, l = [], []
        for i in range(1, len(c)):
            ch = c[i] - c[i-1]
            g.append(max(ch, 0)); l.append(max(-ch, 0))
        ag = sum(g[-p:]) / p; al = sum(l[-p:]) / p
        if al == 0: return 100
        return round(100 - 100 / (1 + ag / al), 1)
    def vix_scale(v):
        if v <= 15: return 1.0
        if v >= 35: return 0.0
        return 1.0 - (v - 15) / 20.0
    BACKTEST_UNIVERSE = {}
    SECTOR_ETFS = {}

# Sector mapping (mirrors apex-relative-strength.py)
INSTRUMENT_SECTOR = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "GOOGL": "Technology", "AMZN": "Technology", "META": "Technology",
    "JPM": "Financials", "GS": "Financials", "V": "Financials",
    "JNJ": "Healthcare", "ABBV": "Healthcare",
    "XOM": "Energy", "CVX": "Energy", "SHEL": "Energy",
    "HSBA": "UK", "AZN": "UK", "GSK": "UK", "ULVR": "UK",
    "VUAG": "Broad",
}

# Sector ETF benchmarks for RS calculation
SECTOR_BENCHMARK_YAHOO = {
    "Technology": "XLK", "Financials": "XLF", "Healthcare": "XLV",
    "Energy": "XLE", "Consumer": "XLP", "UK": "EWU", "Broad": "SPY",
}

# FRED series IDs (matches apex-fred-macro.py exactly)
FRED_SERIES = {
    "FEDFUNDS": "Fed Funds Rate",
    "CPIAUCSL": "CPI Urban",
    "UNRATE": "Unemployment Rate",
    "T10Y2Y": "10Y-2Y Yield Spread",
    "UMCSENT": "Consumer Sentiment",
    "GDPC1": "Real GDP",
    "ICSA": "Initial Jobless Claims",
}

FRED_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_CACHE_DIR = os.path.expanduser("~/.picoclaw/data/fred_cache")
FRED_HEADERS = {"User-Agent": "Apex Trading System apex@localhost"}


# ---------------------------------------------------------------------------
# FRED Data Fetcher (with disk cache)
# ---------------------------------------------------------------------------
def fetch_fred_series(series_id: str, force_refresh: bool = False) -> list:
    """
    Fetch a FRED series as list of (date_str, value) tuples.
    Caches to disk; refreshes if cache > 7 days old.
    """
    os.makedirs(FRED_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(FRED_CACHE_DIR, f"{series_id}.json")

    # Check cache
    if not force_refresh and os.path.exists(cache_path):
        age_days = (time.time() - os.path.getmtime(cache_path)) / 86400
        if age_days < 7:
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

    # Fetch from FRED
    url = FRED_BASE_URL.format(series=series_id)
    try:
        r = requests.get(url, headers=FRED_HEADERS, timeout=15)
        r.raise_for_status()
        rows = []
        for line in r.text.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) != 2:
                continue
            date_str, val_str = parts[0].strip(), parts[1].strip()
            if val_str in ("", ".", "NA"):
                continue
            try:
                rows.append([date_str, float(val_str)])
            except ValueError:
                continue

        # Cache to disk
        with open(cache_path, 'w') as f:
            json.dump(rows, f)
        return rows
    except Exception as e:
        print(f"  FRED {series_id}: fetch failed — {e}")
        # Return cached data even if stale
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return []


def fetch_all_fred() -> dict:
    """Fetch all FRED series. Returns {series_id: [(date_str, value), ...]}."""
    data = {}
    for series_id in FRED_SERIES:
        rows = fetch_fred_series(series_id)
        if rows:
            data[series_id] = rows
        time.sleep(0.3)  # Rate limiting
    return data


# ---------------------------------------------------------------------------
# Helper: binary search for most recent observation <= target date
# ---------------------------------------------------------------------------
def _find_latest_before(series: list, target_date: str) -> int:
    """
    Binary search in sorted [(date, value), ...] for the last entry <= target_date.
    Returns index, or -1 if none found.
    """
    dates = [row[0] for row in series]
    idx = bisect_right(dates, target_date) - 1
    return idx if idx >= 0 else -1


def _get_price_structure_from_closes(closes):
    """Detect higher highs/lows or lower highs/lows from recent 20 closes."""
    if len(closes) < 10:
        return "NEUTRAL", 0
    highs, lows = [], []
    for i in range(2, len(closes) - 2):
        if (closes[i] > closes[i-1] and closes[i] > closes[i-2] and
                closes[i] > closes[i+1] and closes[i] > closes[i+2]):
            highs.append(closes[i])
        if (closes[i] < closes[i-1] and closes[i] < closes[i-2] and
                closes[i] < closes[i+1] and closes[i] < closes[i+2]):
            lows.append(closes[i])
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1] > highs[-2]
        hl = lows[-1] > lows[-2]
        lh = highs[-1] < highs[-2]
        ll = lows[-1] < lows[-2]
        if hh and hl: return "UPTREND", 2
        if lh and ll: return "DOWNTREND", -2
    return "NEUTRAL", 0


# ---------------------------------------------------------------------------
# BacktestIntelligenceV2
# ---------------------------------------------------------------------------
class BacktestIntelligenceV2(BacktestIntelligence):
    """
    Enriched historical intelligence with 4 reconstructed layers.

    Additional constructor args (beyond parent):
        instrument_closes: {ticker: [(date_str, close), ...]}  — daily closes
        instrument_weekly: {ticker: [(date_str, close), ...]}  — weekly closes
        spy_closes:        [(date_str, close), ...]            — SPY daily
        sector_benchmark_closes: {sector: [(date_str, close), ...]}
        fred_data:         {series_id: [(date_str, value), ...]}
    """

    def __init__(self, vix_by_date, spy_data, sector_data,
                 instrument_closes=None, instrument_weekly=None,
                 spy_closes=None, sector_benchmark_closes=None,
                 fred_data=None):
        super().__init__(vix_by_date, spy_data, sector_data)
        self._inst_closes = instrument_closes or {}
        self._inst_weekly = instrument_weekly or {}
        self._spy_closes = spy_closes or []
        self._sector_bm = sector_benchmark_closes or {}
        self._fred = fred_data or {}

        # Pre-index for O(1) date lookups
        self._inst_idx = {t: {d: i for i, (d, _) in enumerate(v)}
                          for t, v in self._inst_closes.items()}
        self._inst_weekly_idx = {t: {d: i for i, (d, _) in enumerate(v)}
                                 for t, v in self._inst_weekly.items()}
        self._spy_closes_idx = {d: i for i, (d, _) in enumerate(self._spy_closes)}
        self._sector_bm_idx = {s: {d: i for i, (d, _) in enumerate(v)}
                               for s, v in self._sector_bm.items()}

    # -------------------------------------------------------------------
    # Layer 1: Relative Strength (mirrors apex-relative-strength.py)
    # -------------------------------------------------------------------
    def _rs_adjustment(self, ticker: str, date_str: str, signal_type: str) -> tuple:
        """
        Compute RS class from 1M and 3M returns vs SPY.
        Returns (adjustment, reason_string).
        """
        inst_data = self._inst_closes.get(ticker, [])
        inst_idx = self._inst_idx.get(ticker, {})
        idx = inst_idx.get(date_str)
        if idx is None or idx < 63:
            return 0, ""

        # Stock returns
        stock_1m = self._pct_return(inst_data, idx, 21)
        stock_3m = self._pct_return(inst_data, idx, 63)

        # SPY returns
        spy_idx = self._spy_closes_idx.get(date_str)
        if spy_idx is None or spy_idx < 63:
            return 0, ""
        market_1m = self._pct_return(self._spy_closes, spy_idx, 21)
        market_3m = self._pct_return(self._spy_closes, spy_idx, 63)

        if market_1m is None or stock_1m is None:
            return 0, ""

        vs_market = round(stock_1m - market_1m, 2)
        vs_market_3m = round((stock_3m or 0) - (market_3m or 0), 2)

        # Composite RS score (mirrors apex-relative-strength.py:calculate_rs_score)
        rs_score = 0
        if vs_market > 3:    rs_score += 2
        elif vs_market > 0:  rs_score += 1
        elif vs_market < -3: rs_score -= 2
        elif vs_market < 0:  rs_score -= 1
        if vs_market_3m > 5:  rs_score += 1
        elif vs_market_3m < -5: rs_score -= 1

        # Classify
        if rs_score >= 3:   rs_class = "STRONG_LEADER"
        elif rs_score >= 1: rs_class = "LEADER"
        elif rs_score >= -1: rs_class = "IN_LINE"
        elif rs_score >= -2: rs_class = "LAGGARD"
        else:               rs_class = "STRONG_LAGGARD"

        # Signal-type adjustment (mirrors get_signal_adjustment)
        if signal_type == 'TREND':
            adj_map = {'STRONG_LEADER': 2, 'LEADER': 1, 'IN_LINE': 0,
                       'LAGGARD': -1, 'STRONG_LAGGARD': -2}
        elif signal_type == 'CONTRARIAN':
            adj_map = {'STRONG_LEADER': 1, 'LEADER': 1, 'IN_LINE': 0,
                       'LAGGARD': 0, 'STRONG_LAGGARD': -1}
        elif signal_type == 'INVERSE':
            adj_map = {'STRONG_LAGGARD': 1, 'LAGGARD': 1, 'IN_LINE': 0,
                       'LEADER': -1, 'STRONG_LEADER': -1}
        else:
            adj_map = {}

        adj = adj_map.get(rs_class, 0)
        reason = f"RS {rs_class} (1M vs SPY: {vs_market:+.1f}%)" if adj != 0 else ""
        return adj, reason

    def _pct_return(self, data, idx, lookback):
        """Percentage return from idx-lookback to idx."""
        if idx < lookback:
            return None
        _, current = data[idx]
        _, prev = data[idx - lookback]
        if prev == 0:
            return None
        return round((current - prev) / prev * 100, 2)

    # -------------------------------------------------------------------
    # Layer 2: Multi-Timeframe (mirrors apex-multiframe.py)
    # -------------------------------------------------------------------
    def _mtf_adjustment(self, ticker: str, date_str: str, signal_type: str) -> tuple:
        """
        Weekly trend alignment check.
        Returns (adjustment, reason_string).
        """
        weekly_data = self._inst_weekly.get(ticker, [])
        weekly_idx_map = self._inst_weekly_idx.get(ticker, {})

        # Find the most recent weekly bar <= date_str
        if not weekly_data:
            return 0, ""

        # Find closest weekly date
        w_idx = None
        for d, i in weekly_idx_map.items():
            if d <= date_str:
                if w_idx is None or i > w_idx:
                    w_idx = i

        if w_idx is None or w_idx < 50:
            return 0, ""

        # Extract weekly closes up to this point
        weekly_closes = [c for _, c in weekly_data[:w_idx + 1]]

        # Calculate weekly EMAs and trend score
        ema21 = calculate_ema(weekly_closes[-21:], 21) if len(weekly_closes) >= 21 else weekly_closes[-1]
        ema50 = calculate_ema(weekly_closes[-50:], 50) if len(weekly_closes) >= 50 else weekly_closes[-1]
        ema200 = calculate_ema(weekly_closes, 200) if len(weekly_closes) >= 200 else calculate_ema(weekly_closes, len(weekly_closes))
        rsi = calculate_rsi(weekly_closes[-28:]) if len(weekly_closes) >= 15 else 50

        price = weekly_closes[-1]
        above_200 = price > ema200
        above_50 = price > ema50
        ema_aligned = ema21 > ema50 > ema200

        # Price structure from last 20 weekly bars
        structure, _ = _get_price_structure_from_closes(weekly_closes[-20:])

        # Trend score (mirrors analyse_timeframe)
        trend_score = 0
        if ema_aligned:                      trend_score += 3
        elif above_50 and above_200:         trend_score += 2
        elif above_200:                      trend_score += 1
        elif not above_200 and not above_50: trend_score -= 2
        elif not above_200:                  trend_score -= 1

        if structure == "UPTREND":   trend_score += 2
        elif structure == "DOWNTREND": trend_score -= 2

        if rsi > 60:   trend_score += 1
        elif rsi < 40: trend_score -= 1

        # Classify
        if trend_score >= 4:   w_class = "STRONG_BULL"
        elif trend_score >= 2: w_class = "BULL"
        elif trend_score >= 0: w_class = "NEUTRAL"
        elif trend_score >= -2: w_class = "BEAR"
        else:                  w_class = "STRONG_BEAR"

        # Signal adjustment (mirrors get_signal_adjustment)
        adj = 0
        if signal_type == 'TREND':
            if w_class in ('STRONG_BULL', 'BULL'):  adj = 2
            elif w_class == 'BEAR':                  adj = -2
            elif w_class == 'STRONG_BEAR':           adj = -3
        elif signal_type in ('CONTRARIAN', 'DIVIDEND_CAPTURE'):
            if w_class in ('STRONG_BULL', 'BULL'):  adj = 2
            elif w_class == 'BEAR':                  adj = -1
            elif w_class == 'STRONG_BEAR':           adj = -2
        elif signal_type == 'INVERSE':
            if w_class in ('STRONG_BEAR', 'BEAR'):  adj = 2
            elif w_class in ('BULL', 'STRONG_BULL'): adj = -2

        reason = f"MTF Weekly {w_class}" if adj != 0 else ""
        return adj, reason

    # -------------------------------------------------------------------
    # Layer 3: FRED Macro (mirrors apex-fred-macro.py:score_fred_data)
    # -------------------------------------------------------------------
    def _fred_adjustment(self, date_str: str) -> tuple:
        """
        Historical FRED scoring. For each series, finds the most recent
        observation <= date_str and applies the same thresholds.
        Returns (adjustment, reason_string).
        """
        if not self._fred:
            return 0, ""

        score = 0
        reasons = []

        # Fed Funds Rate
        ff = self._fred.get("FEDFUNDS", [])
        ff_idx = _find_latest_before(ff, date_str)
        if ff_idx >= 1:
            ff_cur = ff[ff_idx][1]
            ff_prev = ff[ff_idx - 1][1]
            ff_chg = round(ff_cur - ff_prev, 2)
            if ff_cur < 3.0:
                score += 1
                reasons.append(f"Fed {ff_cur:.1f}% easy")
            elif ff_cur > 5.0 and ff_chg >= 0:
                score -= 1
                reasons.append(f"Fed {ff_cur:.1f}% tight")
            elif ff_chg < -0.1:
                score += 1
                reasons.append(f"Fed cutting {ff_chg:+.2f}%")

        # CPI YoY
        cpi = self._fred.get("CPIAUCSL", [])
        cpi_idx = _find_latest_before(cpi, date_str)
        if cpi_idx >= 13:
            cpi_yoy = round((cpi[cpi_idx][1] - cpi[cpi_idx - 12][1]) /
                            abs(cpi[cpi_idx - 12][1]) * 100, 2)
            if cpi_yoy < 2.5:
                score += 1
                reasons.append(f"CPI {cpi_yoy:.1f}% contained")
            elif cpi_yoy > 4.0:
                score -= 1
                reasons.append(f"CPI {cpi_yoy:.1f}% elevated")
            # Re-acceleration check
            if cpi_idx >= 14:
                prev_yoy = round((cpi[cpi_idx - 1][1] - cpi[cpi_idx - 13][1]) /
                                 abs(cpi[cpi_idx - 13][1]) * 100, 2)
                if cpi_yoy > prev_yoy + 0.3:
                    score -= 1
                    reasons.append("CPI re-accelerating")

        # Unemployment
        ur = self._fred.get("UNRATE", [])
        ur_idx = _find_latest_before(ur, date_str)
        if ur_idx >= 3:
            ur_cur = ur[ur_idx][1]
            ur_3m = ur[ur_idx - 3][1]
            ur_trend = round(ur_cur - ur_3m, 2)
            if ur_cur < 4.5 and ur_trend <= 0.1:
                score += 1
                reasons.append(f"Unemployment {ur_cur:.1f}% tight")
            elif ur_trend > 0.3:
                score -= 1
                reasons.append(f"Unemployment rising +{ur_trend:.1f}%")

        # Yield curve
        yc = self._fred.get("T10Y2Y", [])
        yc_idx = _find_latest_before(yc, date_str)
        if yc_idx >= 0:
            yc_cur = yc[yc_idx][1]
            if yc_cur > 0.25:
                score += 1
                reasons.append(f"Yield +{yc_cur:.2f}% supportive")
            elif yc_cur < -0.2:
                score -= 1
                reasons.append(f"Yield {yc_cur:.2f}% inverted")

        # Consumer sentiment
        cs = self._fred.get("UMCSENT", [])
        cs_idx = _find_latest_before(cs, date_str)
        if cs_idx >= 1:
            cs_chg = round(cs[cs_idx][1] - cs[cs_idx - 1][1], 1)
            if cs_chg > 3.0:
                score += 1
                reasons.append(f"Sentiment +{cs_chg}pts")
            elif cs_chg < -5.0:
                score -= 1
                reasons.append(f"Sentiment {cs_chg}pts")

        # Jobless claims
        ic = self._fred.get("ICSA", [])
        ic_idx = _find_latest_before(ic, date_str)
        if ic_idx >= 1:
            ic_cur, ic_prev = ic[ic_idx][1], ic[ic_idx - 1][1]
            ic_chg_pct = round((ic_cur - ic_prev) / ic_prev * 100, 1) if ic_prev else 0
            if ic_chg_pct > 10.0:
                score -= 1
                reasons.append(f"Claims +{ic_chg_pct:.1f}%")
            elif ic_chg_pct < -5.0:
                score += 1
                reasons.append(f"Claims {ic_chg_pct:.1f}%")

        # GDP
        gdp = self._fred.get("GDPC1", [])
        gdp_idx = _find_latest_before(gdp, date_str)
        if gdp_idx >= 1:
            gdp_qoq = round((gdp[gdp_idx][1] - gdp[gdp_idx - 1][1]) /
                            abs(gdp[gdp_idx - 1][1]) * 100 * 4, 2)
            if gdp_qoq > 2.5:
                score += 1
                reasons.append(f"GDP {gdp_qoq:.1f}% strong")
            elif gdp_qoq < 0:
                score -= 1
                reasons.append(f"GDP {gdp_qoq:.1f}% contraction")

        # Cap to [-3, +3] as in live system
        score = max(-3, min(3, score))

        # Map to scoring adjustment: FRED score ±3 → layer adjustment ±1
        # (live system applies ±1 from FRED)
        if score >= 2:
            adj = 1
        elif score <= -2:
            adj = -1
        else:
            adj = 0

        reason = f"FRED {score:+d}" + (f" ({'; '.join(reasons[:2])})" if reasons else "")
        return adj, reason if adj != 0 else ""

    # -------------------------------------------------------------------
    # Layer 4: Sentiment Proxy (VIX 5-day delta)
    # -------------------------------------------------------------------
    def _sentiment_proxy(self, date_str: str) -> tuple:
        """
        Approximate market sentiment from VIX dynamics.
        VIX ↑5pts in 5 days → -1 (fear spike)
        VIX ↓3pts in 5 days → +1 (complacency returning)
        """
        vix_now = self._vix.get(date_str)
        if vix_now is None:
            return 0, ""

        # Find VIX 5 trading days ago
        spy_idx = self._spy_idx.get(date_str)
        if spy_idx is None or spy_idx < 5:
            return 0, ""

        date_5d_ago = self._spy[spy_idx - 5][0]
        vix_5d = self._vix.get(date_5d_ago)
        if vix_5d is None:
            return 0, ""

        delta = vix_now - vix_5d

        if delta >= 5:
            return -1, f"VIX +{delta:.1f}pts (fear spike)"
        elif delta <= -3:
            return 1, f"VIX {delta:.1f}pts (sentiment improving)"
        return 0, ""

    # -------------------------------------------------------------------
    # Override: get_intel with enriched layers
    # -------------------------------------------------------------------
    def get_intel(self, date_str: str, ticker: str = '', signal_type: str = 'TREND') -> dict:
        """Build intel dict for a given historical date, enriched with 4 extra layers."""
        intel = super().get_intel(date_str)

        # Attach extra layer adjustments
        rs_adj, rs_reason = self._rs_adjustment(ticker, date_str, signal_type)
        mtf_adj, mtf_reason = self._mtf_adjustment(ticker, date_str, signal_type)
        fred_adj, fred_reason = self._fred_adjustment(date_str)
        sent_adj, sent_reason = self._sentiment_proxy(date_str)

        intel['_bt_v2_layers'] = {
            'rs':        {'adj': rs_adj, 'reason': rs_reason},
            'mtf':       {'adj': mtf_adj, 'reason': mtf_reason},
            'fred':      {'adj': fred_adj, 'reason': fred_reason},
            'sentiment': {'adj': sent_adj, 'reason': sent_reason},
        }
        intel['_bt_v2_total_adj'] = rs_adj + mtf_adj + fred_adj + sent_adj

        return intel


# ---------------------------------------------------------------------------
# Data fetcher: one-shot download of all required historical data
# ---------------------------------------------------------------------------
def fetch_backtest_data(universe: dict = None, period: str = "5y"):
    """
    Download all data needed for BacktestIntelligenceV2.
    Returns dict of pre-fetched data ready for the constructor.

    universe: {name: yahoo_ticker} — defaults to BACKTEST_UNIVERSE
    """
    import yfinance as yf

    if universe is None:
        universe = BACKTEST_UNIVERSE

    def _fix_pence(price, yahoo):
        return price / 100 if (yahoo.endswith('.L') and price > 100) else price

    print("  Fetching historical data for backtest v2...")

    # VIX
    vix_by_date = {}
    try:
        vix_hist = yf.Ticker("^VIX").history(period=period)
        vix_by_date = {d.strftime('%Y-%m-%d'): float(v)
                       for d, v in zip(vix_hist.index, vix_hist['Close'])}
        print(f"    VIX: {len(vix_by_date)} days")
    except Exception as e:
        print(f"    VIX: failed ({e})")

    # SPY (daily + weekly)
    spy_data = []
    spy_closes = []
    try:
        spy_hist = yf.Ticker("SPY").history(period=period)
        spy_data = [(d.strftime('%Y-%m-%d'), float(c))
                    for d, c in zip(spy_hist.index, spy_hist['Close'])]
        spy_closes = spy_data  # Same format for RS calculation
        print(f"    SPY: {len(spy_data)} days")
    except Exception as e:
        print(f"    SPY: failed ({e})")

    # Sector ETFs (for parent class)
    sector_data = {}
    for sector, etf in SECTOR_ETFS.items():
        try:
            h = yf.Ticker(etf).history(period=period)
            sector_data[sector] = [(d.strftime('%Y-%m-%d'), float(c))
                                   for d, c in zip(h.index, h['Close'])]
        except Exception:
            sector_data[sector] = []
    loaded = sum(1 for v in sector_data.values() if v)
    print(f"    Sector ETFs: {loaded} loaded")

    # Sector benchmarks for RS
    sector_bm = {}
    for sector, etf in SECTOR_BENCHMARK_YAHOO.items():
        if etf == "SPY":
            sector_bm[sector] = spy_closes
            continue
        try:
            h = yf.Ticker(etf).history(period=period)
            sector_bm[sector] = [(d.strftime('%Y-%m-%d'),
                                  _fix_pence(float(c), etf))
                                 for d, c in zip(h.index, h['Close'])]
        except Exception:
            sector_bm[sector] = []
    print(f"    Sector benchmarks: {sum(1 for v in sector_bm.values() if v)} loaded")

    # Instruments (daily)
    instrument_closes = {}
    for name, yahoo in universe.items():
        try:
            h = yf.Ticker(yahoo).history(period=period)
            if not h.empty:
                instrument_closes[name] = [
                    (d.strftime('%Y-%m-%d'), _fix_pence(float(c), yahoo))
                    for d, c in zip(h.index, h['Close'])
                ]
        except Exception:
            pass
    print(f"    Instruments (daily): {len(instrument_closes)}/{len(universe)}")

    # Instruments (weekly) for MTF
    instrument_weekly = {}
    for name, yahoo in universe.items():
        try:
            h = yf.Ticker(yahoo).history(period=period, interval="1wk")
            if not h.empty:
                instrument_weekly[name] = [
                    (d.strftime('%Y-%m-%d'), _fix_pence(float(c), yahoo))
                    for d, c in zip(h.index, h['Close'])
                ]
        except Exception:
            pass
    print(f"    Instruments (weekly): {len(instrument_weekly)}/{len(universe)}")

    # FRED data
    print("    Fetching FRED series...")
    fred_data = fetch_all_fred()
    print(f"    FRED: {len(fred_data)}/{len(FRED_SERIES)} series loaded")

    return {
        'vix_by_date': vix_by_date,
        'spy_data': spy_data,
        'sector_data': sector_data,
        'instrument_closes': instrument_closes,
        'instrument_weekly': instrument_weekly,
        'spy_closes': spy_closes,
        'sector_benchmark_closes': sector_bm,
        'fred_data': fred_data,
    }


def build_intelligence(data: dict) -> 'BacktestIntelligenceV2':
    """Construct BacktestIntelligenceV2 from fetch_backtest_data() output."""
    return BacktestIntelligenceV2(
        vix_by_date=data['vix_by_date'],
        spy_data=data['spy_data'],
        sector_data=data['sector_data'],
        instrument_closes=data['instrument_closes'],
        instrument_weekly=data['instrument_weekly'],
        spy_closes=data['spy_closes'],
        sector_benchmark_closes=data['sector_benchmark_closes'],
        fred_data=data['fred_data'],
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    print("BacktestIntelligenceV2 — Smoke Test")
    print("=" * 50)

    # Test FRED fetch
    print("\n1. Testing FRED fetch (1 series)...")
    ff = fetch_fred_series("FEDFUNDS")
    if ff:
        print(f"   FEDFUNDS: {len(ff)} observations, latest: {ff[-1][0]} = {ff[-1][1]}")
    else:
        print("   FEDFUNDS: FAILED (network issue?)")

    # Test binary search
    test_series = [["2024-01-01", 5.0], ["2024-02-01", 5.1], ["2024-03-01", 4.9]]
    assert _find_latest_before(test_series, "2024-02-15") == 1
    assert _find_latest_before(test_series, "2023-12-01") == -1
    assert _find_latest_before(test_series, "2024-03-01") == 2
    print("\n2. Binary search: PASS")

    # Test FRED scoring with mock data
    print("\n3. Testing FRED scoring logic...")
    bt = BacktestIntelligenceV2(
        vix_by_date={}, spy_data=[], sector_data={},
        fred_data={
            "FEDFUNDS": [["2024-01-01", 2.5], ["2024-02-01", 2.5]],
            "T10Y2Y": [["2024-01-01", 0.5]],
        }
    )
    adj, reason = bt._fred_adjustment("2024-02-15")
    print(f"   FRED adj={adj}, reason={reason}")
    # FF < 3.0 = +1, yield > 0.25 = +1 → score=2 → adj=1
    assert adj == 1, f"Expected adj=1, got {adj}"
    print("   PASS")

    print("\n4. Testing sentiment proxy...")
    # Need spy_data with dates matching vix_by_date for index lookup
    spy_dates = [
        ("2024-01-02", 470), ("2024-01-03", 471), ("2024-01-04", 472),
        ("2024-01-05", 473), ("2024-01-08", 474), ("2024-01-09", 475),
        ("2024-01-10", 476), ("2024-01-11", 477), ("2024-01-12", 478),
    ]
    bt2 = BacktestIntelligenceV2(
        vix_by_date={"2024-01-05": 15, "2024-01-12": 22},
        spy_data=spy_dates,
        sector_data={},
    )
    adj, reason = bt2._sentiment_proxy("2024-01-12")
    print(f"   Sentiment adj={adj}, reason={reason}")
    # spy_idx for 2024-01-12 is 8, spy[8-5] = spy[3] = 2024-01-05, VIX 15→22 = +7 → -1
    assert adj == -1, f"Expected -1, got {adj}"
    print("   PASS")

    print("\n" + "=" * 50)
    print("ALL SMOKE TESTS PASSED")
