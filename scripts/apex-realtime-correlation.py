#!/usr/bin/env python3
"""
Real-time correlation check.
Runs before every new position to check correlation with existing positions.
More frequent than weekly matrix — catches intra-week correlation spikes.
"""
import yfinance as yf
import json
import sys
from datetime import datetime, timezone

POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
CORRELATION_FILE = '/home/ubuntu/.picoclaw/logs/apex-portfolio-correlation.json'

YAHOO_MAP = {
    "VUAGl_EQ":   "VUAG.L",
    "XOM_US_EQ":  "XOM",
    "V_US_EQ":    "V",
    "AAPL_US_EQ": "AAPL",
    "MSFT_US_EQ": "MSFT",
    "NVDA_US_EQ": "NVDA",
    "GOOGL_US_EQ":"GOOGL",
    "JPM_US_EQ":  "JPM",
    "GS_US_EQ":   "GS",
    "SHEL_EQ":    "SHEL.L",
    "HSBA_EQ":    "HSBA.L",
    "AZN_EQ":     "AZN.L",
    "ABBV_US_EQ": "ABBV",
    "JNJ_US_EQ":  "JNJ",
    "CVX_US_EQ":  "CVX",
}

# Auto-detect yahoo ticker from instrument name
def get_yahoo(ticker, name=""):
    if ticker in YAHOO_MAP:
        return YAHOO_MAP[ticker]
    # Try to construct from ticker
    clean = ticker.replace('_US_EQ','').replace('l_EQ','').replace('_EQ','')
    # UK stocks
    if any(uk in name for uk in ['Vanguard','Shell','HSBC','AstraZeneca','GSK','Unilever','Barclays']):
        return clean + '.L'
    return clean

def get_returns(yahoo_ticker, period="3mo"):
    try:
        hist = yf.Ticker(yahoo_ticker).history(period=period)
        if hist.empty:
            return None
        close = hist['Close']
        if close.iloc[-1] > 500 and yahoo_ticker.endswith('.L'):
            close = close / 100
        ret = close.pct_change().dropna()
        ret.index = ret.index.tz_localize(None) if ret.index.tz else ret.index
        ret.index = ret.index.normalize()
        return ret
    except:
        return None

def _get_dynamic_threshold(default_threshold=0.65):
    """
    Return a VIX-adjusted correlation threshold.
    During market stress all correlations converge to 1.0 — tighten the threshold
    so that only truly uncorrelated positions are allowed.

    VIX < 20  → 0.65 (normal)
    VIX 20-25 → 0.55 (elevated)
    VIX 25-30 → 0.45 (high stress)
    VIX > 30  → 0.35 (crisis — extremely restrictive)
    """
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-regime.json') as f:
            regime = json.load(f)
        vix = float(regime.get('vix', 20) or 20)
    except Exception:
        return default_threshold, 20.0

    if vix < 20:
        threshold = 0.65
    elif vix < 25:
        threshold = 0.55
    elif vix < 30:
        threshold = 0.45
    else:
        threshold = 0.35

    return threshold, vix


def check_new_position_correlation(new_ticker, new_yahoo, threshold=None):
    """
    Check if a new instrument is too correlated with existing positions.
    Uses dynamic VIX-based threshold — tightens during market stress.
    Returns: is_blocked, max_correlation, correlated_with
    """
    # Apply dynamic VIX-based threshold if no explicit threshold given
    dynamic_threshold, vix = _get_dynamic_threshold()
    effective_threshold = threshold if threshold is not None else dynamic_threshold

    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        return False, 0, []

    if not positions:
        return False, 0, []

    import pandas as pd

    new_returns = get_returns(new_yahoo)
    if new_returns is None:
        return False, 0, []

    high_correlations = []
    max_corr = 0

    for pos in positions:
        ticker  = pos.get('t212_ticker','')
        name    = pos.get('name','')
        sector  = pos.get('sector','').lower()
        yahoo   = get_yahoo(ticker, name)

        pos_returns = get_returns(yahoo)
        if pos_returns is None:
            continue

        # Align
        aligned = pd.DataFrame({
            'new': new_returns,
            'pos': pos_returns
        }).dropna()

        if len(aligned) < 20:
            continue

        corr = round(float(aligned['new'].corr(aligned['pos'])), 2)

        if abs(corr) > max_corr:
            max_corr = abs(corr)

        # In high stress, treat same-sector positions as highly correlated
        # regardless of calculated correlation (all sectors converge in a crash)
        effective_corr = corr
        if vix > 25 and sector and sector not in ('etf', 'unknown', ''):
            try:
                from apex_utils import safe_read as _sr
                _positions_full = _sr(POSITIONS_FILE, [])
                new_pos_sector = ''
                for p in _positions_full:
                    if p.get('t212_ticker') == new_ticker:
                        new_pos_sector = p.get('sector', '').lower()
                        break
                if new_pos_sector and new_pos_sector == sector:
                    effective_corr = max(abs(corr), 0.90)
            except Exception:
                pass

        if abs(effective_corr) >= effective_threshold:
            high_correlations.append({
                'position':  name,
                'ticker':    ticker,
                'corr':      corr,
                'effective': effective_corr,
                'threshold': effective_threshold,
                'vix':       vix,
            })

    is_blocked = len(high_correlations) > 0
    return is_blocked, max_corr, high_correlations

def run_portfolio_correlation():
    """Run full portfolio correlation matrix and save."""
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except:
        return

    if len(positions) < 2:
        return

    import pandas as pd

    returns_data = {}
    for pos in positions:
        ticker = pos.get('t212_ticker','')
        name   = pos.get('name', ticker)
        yahoo  = get_yahoo(ticker, name)
        ret    = get_returns(yahoo)
        if ret is not None:
            returns_data[name] = ret

    if len(returns_data) < 2:
        return

    df   = pd.DataFrame(returns_data).ffill().dropna()
    corr = df.corr()

    now     = datetime.now(timezone.utc)
    pairs   = []
    warnings = []

    names = list(returns_data.keys())
    for i, n1 in enumerate(names):
        for j, n2 in enumerate(names):
            if j <= i:
                continue
            try:
                c = round(float(corr.loc[n1, n2]), 2)
                risk = "HIGH" if abs(c) > 0.7 else ("MEDIUM" if abs(c) > 0.5 else "LOW")
                pairs.append({'pair': f"{n1}/{n2}", 'correlation': c, 'risk': risk})
                if abs(c) > 0.7:
                    warnings.append(f"{n1} & {n2}: {c:+.2f} — highly correlated")
            except:
                pass

    output = {
        'timestamp': now.strftime('%Y-%m-%d %H:%M UTC'),
        'pairs':     pairs,
        'warnings':  warnings,
        'overall':   'HIGH_RISK' if warnings else 'DIVERSIFIED'
    }

    with open(CORRELATION_FILE, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n=== PORTFOLIO CORRELATION ===")
    for p in sorted(pairs, key=lambda x: abs(x['correlation']), reverse=True):
        icon = "🔴" if p['risk'] == 'HIGH' else ("🟡" if p['risk'] == 'MEDIUM' else "✅")
        print(f"  {icon} {p['pair']:35} | {p['correlation']:+.2f} | {p['risk']}")

    if warnings:
        print(f"\n⚠️ Warnings:")
        for w in warnings:
            print(f"  {w}")
    else:
        print(f"\n✅ Portfolio well diversified")

CRASH_MODE_FILE = '/home/ubuntu/.picoclaw/logs/apex-crash-mode.json'

def check_crash_correlation(intraday_period='5d'):
    """
    Intraday correlation crash detector.
    Computes average pairwise correlation across all positions using recent data.
    If average correlation > 0.85, this indicates a crash/panic where everything
    moves together — system enters CRASH_MODE and cuts sizing to 25%.

    Returns (is_crash, avg_corr, details).
    """
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    except Exception:
        return False, 0.0, {}

    if len(positions) < 2:
        return False, 0.0, {}

    import pandas as pd

    returns_data = {}
    for pos in positions:
        ticker = pos.get('t212_ticker', '')
        name   = pos.get('name', ticker)
        yahoo  = get_yahoo(ticker, name)
        ret    = get_returns(yahoo, period=intraday_period)
        if ret is not None:
            returns_data[name] = ret

    if len(returns_data) < 2:
        return False, 0.0, {}

    df   = pd.DataFrame(returns_data).ffill().dropna()
    corr = df.corr()

    names = list(returns_data.keys())
    pair_corrs = []
    for i, n1 in enumerate(names):
        for j, n2 in enumerate(names):
            if j <= i:
                continue
            try:
                c = abs(float(corr.loc[n1, n2]))
                pair_corrs.append(c)
            except Exception:
                pass

    if not pair_corrs:
        return False, 0.0, {}

    avg_corr = round(sum(pair_corrs) / len(pair_corrs), 3)
    is_crash = avg_corr > 0.85

    now    = datetime.now(timezone.utc)
    details = {
        'timestamp':    now.isoformat(),
        'avg_corr':     avg_corr,
        'pair_count':   len(pair_corrs),
        'is_crash_mode': is_crash,
        'threshold':    0.85,
        'sizing_mult':  0.25 if is_crash else 1.0,
    }

    # Load previous state to detect transitions
    prev = {}
    try:
        with open(CRASH_MODE_FILE) as f:
            prev = json.load(f)
    except Exception:
        pass

    with open(CRASH_MODE_FILE, 'w') as f:
        json.dump(details, f, indent=2)

    # Alert on transition into crash mode
    was_crash = prev.get('is_crash_mode', False)
    if is_crash and not was_crash:
        try:
            sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
            from apex_utils import send_telegram
            send_telegram(
                f"🔴 CRASH MODE ACTIVATED\n\n"
                f"Portfolio avg correlation: {avg_corr:.2f} (threshold 0.85)\n"
                f"All positions moving together — classic crash pattern.\n\n"
                f"Position sizing cut to 25% until correlation normalises.\n"
                f"Check circuit breaker and consider reducing positions."
            )
        except Exception:
            pass

    return is_crash, avg_corr, details


def get_crash_mode_multiplier():
    """Quick read of crash mode state — used by position sizer."""
    try:
        with open(CRASH_MODE_FILE) as f:
            data = json.load(f)
        if data.get('is_crash_mode', False):
            return 0.25, 'CRASH_MODE'
        return 1.0, 'NORMAL'
    except Exception:
        return 1.0, 'NORMAL'


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'portfolio'

    if mode == 'portfolio':
        run_portfolio_correlation()
    elif mode == 'crash':
        is_crash, avg_corr, details = check_crash_correlation()
        print(f"Avg correlation: {avg_corr:.3f}")
        print(f"Crash mode: {'YES' if is_crash else 'NO'}")
        if is_crash:
            print(f"  Sizing multiplier: 0.25x (25%)")
    elif mode == 'check' and len(sys.argv) >= 4:
        ticker = sys.argv[2]
        yahoo  = sys.argv[3]
        blocked, max_corr, high = check_new_position_correlation(ticker, yahoo)
        print(f"Ticker: {ticker} | Max correlation: {max_corr:.2f}")
        if blocked:
            print(f"BLOCKED — too correlated:")
            for h in high:
                print(f"  {h['position']}: {h['corr']:+.2f}")
        else:
            print(f"CLEAR — acceptable correlation")
