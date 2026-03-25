#!/usr/bin/env python3
"""
Contrarian Quality Gates
Hard gates that must pass before any contrarian signal executes.

Gate 1: Earnings estimate trajectory — estimates must be stabilising or rising
Gate 2: Catalyst identification — need a reason for recovery
Gate 3: Fundamental floor — stock must be cheap on fundamentals
Gate 4: Sector mean reversion eligibility — only trade sectors that mean revert
Gate 5: Staged entry calculation — initial 50% + add-on plan
"""
import json
import urllib.request
import urllib.parse
import yfinance as yf
from datetime import datetime, timezone, timedelta
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


GATES_FILE  = '/home/ubuntu/.picoclaw/logs/apex-contrarian-gates.json'
SIGNALS_FILE = '/home/ubuntu/.picoclaw/logs/apex-fundamental-signals.json'
FUND_FILE   = '/home/ubuntu/.picoclaw/logs/apex-fundamentals.json'

def _get_api_key():
    try:
        from apex_config import get_env
        return get_env('FMP_API_KEY', '')
    except ImportError:
        try:
            with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
                for line in f:
                    if line.strip().startswith('FMP_API_KEY='):
                        return line.strip().split('=', 1)[1]
        except Exception:
            pass
        return ''

API_KEY = _get_api_key()
BASE    = 'https://financialmodelingprep.com/stable'

YAHOO_MAP = {
    "AAPL":"AAPL","MSFT":"MSFT","NVDA":"NVDA","GOOGL":"GOOGL",
    "AMZN":"AMZN","META":"META","JPM":"JPM","GS":"GS",
    "V":"V","BAC":"BAC","BLK":"BLK","JNJ":"JNJ","PFE":"PFE",
    "UNH":"UNH","ABBV":"ABBV","XOM":"XOM","CVX":"CVX",
    "KO":"KO","PEP":"PEP","PG":"PG","WMT":"WMT",
    "HSBA":"HSBA.L","AZN":"AZN.L","GSK":"GSK.L",
    "ULVR":"ULVR.L","SHEL":"SHEL.L",
}

# Sectors that historically mean revert reliably
MEAN_REVERT_SECTORS = [
    "Energy", "Consumer Staples", "Healthcare",
    "Utilities", "Materials", "Industrials",
    "Consumer", "Pharma", "Oil"
]

# Sectors to avoid for contrarian
AVOID_CONTRARIAN_SECTORS = [
    "Technology", "Financials", "Communication Services",
    "Real Estate", "Crypto"
]

# Instrument sector map
INSTRUMENT_SECTOR = {
    "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology",
    "GOOGL":"Technology","AMZN":"Technology","META":"Technology",
    "JPM":"Financials","GS":"Financials","V":"Financials",
    "BAC":"Financials","BLK":"Financials",
    "JNJ":"Healthcare","PFE":"Healthcare","UNH":"Healthcare","ABBV":"Healthcare",
    "XOM":"Energy","CVX":"Energy","SHEL":"Energy",
    "KO":"Consumer Staples","PEP":"Consumer Staples",
    "PG":"Consumer Staples","WMT":"Consumer Staples",
    "HSBA":"Financials","AZN":"Healthcare","GSK":"Healthcare",
    "ULVR":"Consumer Staples",
}

QUOTA_FILE  = '/home/ubuntu/.picoclaw/logs/apex-fmp-quota.json'
DAILY_LIMIT = 230


def _quota_check_and_record():
    """Return True if quota allows another call, and record it."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        q = json.load(open(QUOTA_FILE))
        if q.get('date') != today:
            q = {'date': today, 'calls': 0, 'by_script': {}}
    except Exception:
        q = {'date': today, 'calls': 0, 'by_script': {}}
    if int(q.get('calls', 0)) >= DAILY_LIMIT:
        return False
    q['calls'] = int(q.get('calls', 0)) + 1
    q.setdefault('by_script', {})
    q['by_script']['contrarian-gates'] = q['by_script'].get('contrarian-gates', 0) + 1
    try:
        with open(QUOTA_FILE, 'w') as f:
            json.dump(q, f, indent=2)
    except Exception:
        pass
    return True


def fmp_request(endpoint, params=None):
    if not _quota_check_and_record():
        return None
    p = params or {}
    p['apikey'] = API_KEY
    url = f"{BASE}/{endpoint}?" + urllib.parse.urlencode(p)
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'ApexBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            if isinstance(data, dict) and 'Error Message' in data:
                return None
            return data
    except:
        return None

# ============================================================
# GATE 1: EARNINGS ESTIMATE TRAJECTORY
# Hard gate — estimates must be stabilising or rising
# ============================================================
def check_earnings_trajectory(symbol):
    """
    Uses price target trend as earnings estimate proxy.
    HARD GATE: if targets are being cut aggressively, block the contrarian signal.
    Logic: if analysts are still cutting estimates, the stock hasn't found its floor.
    Only trade when cuts have stopped or reversed.
    """
    try:
        # Load from existing fundamental signals if available
        with open(SIGNALS_FILE) as f:
            sig_data = json.load(f)
        rev_data = sig_data.get('data', {}).get(symbol, {}).get('revisions', {})

        if rev_data:
            trend           = rev_data.get('trend', 'UNKNOWN')
            month_vs_qtr    = rev_data.get('month_vs_qtr_pct', 0)
            month_vs_year   = rev_data.get('month_vs_year_pct', 0)

            # HARD BLOCK conditions
            if trend == 'STRONG_DOWN' and month_vs_qtr < -5:
                return False, f"BLOCKED — estimates still being cut aggressively ({month_vs_year:+.1f}% YoY, {month_vs_qtr:+.1f}% recent)"

            # CAUTION conditions — allow but flag
            if trend == 'DOWN':
                return True, f"CAUTION — estimates declining ({month_vs_year:+.1f}% YoY) — cuts may continue"

            # Clear conditions
            if trend in ['STRONG_UP', 'UP']:
                return True, f"CLEAR — estimates rising ({month_vs_year:+.1f}% YoY) — floor likely in"

            if trend == 'FLAT':
                return True, f"CLEAR — estimates stabilising — cuts appear done"

            return True, "CLEAR — no revision data, proceeding"

    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    # Fallback — check price target direction from FMP
    try:
        data = fmp_request('price-target-summary', {'symbol': symbol})
        if data and isinstance(data, list):
            d = data[0]
            last_month = float(d.get('lastMonthAvgPriceTarget', 0) or 0)
            last_qtr   = float(d.get('lastQuarterAvgPriceTarget', 0) or 0)

            if last_month > 0 and last_qtr > 0:
                change = round((last_month - last_qtr) / last_qtr * 100, 1)
                if change < -10:
                    return False, f"BLOCKED — price targets cut {change:+.1f}% in last month vs quarter"
                return True, f"CLEAR — targets {change:+.1f}% month vs quarter"
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    return True, "CLEAR — no trajectory data available, proceeding"

# ============================================================
# GATE 2: CATALYST IDENTIFICATION
# Soft gate — flags if no catalyst found, doesn't hard block
# ============================================================
def check_catalyst(symbol, yahoo):
    """
    Identifies potential catalysts for recovery:
    1. Earnings announcement within 30 days (market reassessment)
    2. Recent insider buying (smart money positioning)
    3. Analyst upgrades (sentiment turning)
    4. Extreme oversold + sector rotation signal
    """
    catalysts = []
    warnings  = []

    # Catalyst 1: Earnings date
    try:
        t    = yf.Ticker(yahoo)
        cal  = t.calendar
        # yfinance >=0.2 returns dict; older versions returned DataFrame
        if cal is not None and cal:
            earn_key = 'Earnings Date'
            # Support both dict (yf>=0.2) and DataFrame (legacy)
            if isinstance(cal, dict):
                cal_index_check = earn_key in cal
                cal_loc = lambda k: cal[k]
            else:
                cal_index_check = earn_key in cal.index
                cal_loc = lambda k: cal.loc[k]
            if cal_index_check:
                earn_dates = cal_loc(earn_key)
                if hasattr(earn_dates, '__iter__'):
                    for ed in earn_dates:
                        try:
                            import pandas as _pd
                            _ts = _pd.Timestamp(ed)
                            ed_naive  = _ts.tz_convert(None) if _ts.tzinfo else _ts
                            days_away = (ed_naive - datetime.now()).days
                            if 0 <= days_away <= 45:
                                catalysts.append(f"Earnings in {days_away} days — potential reset catalyst")
                            elif days_away > 45:
                                warnings.append(f"Next earnings {days_away} days away — long wait for catalyst")
                        except Exception as _e:
                            log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    # Catalyst 2: Insider buying from fundamental signals
    try:
        with open(SIGNALS_FILE) as f:
            sig_data = json.load(f)
        insider = sig_data.get('data', {}).get(symbol, {}).get('insider', {})
        if insider:
            purchases = insider.get('purchases_90d', 0)
            trend     = insider.get('trend', 'NEUTRAL')
            if trend in ['STRONG_BUY', 'BUY']:
                catalysts.append(f"Insider buying: {purchases} purchases in 90 days — smart money accumulating")
            elif trend == 'SELLING':
                warnings.append("Insider selling detected — insiders bearish on recovery")
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    # Catalyst 3: Extreme RSI as its own catalyst (selling exhaustion)
    try:
        t    = yf.Ticker(yahoo)
        hist = t.history(period="3mo")
        if not hist.empty:
            closes = list(hist['Close'])
            # Simple RSI
            if len(closes) >= 15:
                gains  = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
                losses = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
                ag = sum(gains[-14:])/14
                al = sum(losses[-14:])/14
                rsi = 100 - (100/(1+ag/al)) if al > 0 else 100
                if rsi < 10:
                    catalysts.append(f"RSI {round(rsi,1)} — extreme oversold, selling exhaustion likely catalyst")
                elif rsi < 20:
                    catalysts.append(f"RSI {round(rsi,1)} — deeply oversold, high mean reversion probability")
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    # Catalyst 4: Check RS improvement
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-relative-strength.json') as f:
            rs_data = json.load(f)
        inst_rs = rs_data.get('data', {}).get(symbol, {})
        ret_1w  = inst_rs.get('ret_1w', 0)
        ret_1m  = inst_rs.get('ret_1m', 0)
        if ret_1w > 0 and ret_1m < -5:
            catalysts.append(f"Price stabilising this week (+{ret_1w}%) after {ret_1m}% monthly decline — potential turn")
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    has_catalyst = len(catalysts) > 0

    if not has_catalyst:
        warnings.append("No catalyst identified — recovery trigger unclear")

    return has_catalyst, catalysts, warnings

# ============================================================
# GATE 3: FUNDAMENTAL FLOOR
# Hard gate — stock must be cheap on fundamentals
# ============================================================
def check_fundamental_floor(symbol):
    """
    Checks if stock is genuinely cheap on fundamentals.
    Not just technically oversold — must also be fundamentally cheap.
    
    Requirements:
    - FCF yield > 3% (generating real cash)
    - Payout ratio < 80% if dividend paying
    - Earnings quality not POOR
    """
    gates_passed = []
    gates_failed = []

    try:
        with open(FUND_FILE) as f:
            fund_data = json.load(f)
        inst = fund_data.get('data', {}).get(symbol, {})
        ratios = inst.get('ratios', {}) or {}

        fcf_yield    = float(ratios.get('fcf_yield', 0) or 0)
        ev_ebitda    = float(ratios.get('ev_ebitda', 0) or 0)
        net_margin   = float(ratios.get('net_margin', 0) or 0)
        fund_score   = inst.get('fund_score', 5)

        # FCF yield gate
        if fcf_yield > 0.05:
            gates_passed.append(f"FCF yield {round(fcf_yield*100,1)}% — strong cash generation")
        elif fcf_yield > 0.03:
            gates_passed.append(f"FCF yield {round(fcf_yield*100,1)}% — acceptable")
        elif fcf_yield > 0:
            gates_failed.append(f"FCF yield {round(fcf_yield*100,1)}% — weak, below 3% threshold")
        else:
            # FCF unavailable — don't hard block, just note it
            gates_passed.append("FCF yield unavailable — cannot verify (proceeding)")

        # EV/EBITDA gate
        if 0 < ev_ebitda < 15:
            gates_passed.append(f"EV/EBITDA {ev_ebitda} — cheap, below 15x")
        elif 0 < ev_ebitda < 25:
            gates_passed.append(f"EV/EBITDA {ev_ebitda} — reasonable")
        elif ev_ebitda > 35:
            gates_failed.append(f"EV/EBITDA {ev_ebitda} — expensive even after selloff")

        # Overall fundamental score
        if fund_score >= 7:
            gates_passed.append(f"Fundamental score {fund_score}/10 — quality confirmed")
        elif fund_score <= 4:
            gates_failed.append(f"Fundamental score {fund_score}/10 — weak fundamentals")

    except:
        # No data available — warn but don't block
        # Gate 3 only hard blocks when we have data confirming bad fundamentals
        gates_passed.append("No fundamental data (API limit or unavailable) — proceeding with caution")

    # Check earnings quality
    try:
        with open(SIGNALS_FILE) as f:
            sig_data = json.load(f)
        accruals = sig_data.get('data', {}).get(symbol, {}).get('accruals', {})
        if accruals:
            quality = accruals.get('quality', 'UNKNOWN')
            if quality == 'POOR':
                gates_failed.append(f"Earnings quality POOR — reported earnings not backed by cash")
            elif quality in ['EXCEPTIONAL', 'HIGH']:
                gates_passed.append(f"Earnings quality {quality} — clean earnings")
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    # Payout ratio check
    try:
        with open(SIGNALS_FILE) as f:
            sig_data = json.load(f)
        div = sig_data.get('data', {}).get(symbol, {}).get('dividend', {})
        if div and div.get('has_dividend'):
            payout = div.get('payout_ratio', 0)
            safety = div.get('safety', 'UNKNOWN')
            if payout > 100:
                gates_failed.append(f"Dividend payout {payout}% — cut likely, creates additional selling pressure")
            elif payout > 80:
                gates_failed.append(f"Dividend payout {payout}% — stretched, dividend at risk")
            elif payout > 0:
                gates_passed.append(f"Dividend payout {payout}% ({safety}) — sustainable")
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    # Pass if more gates passed than failed
    critical_failures = len(gates_failed)
    passed            = critical_failures == 0

    return passed, gates_passed, gates_failed

# ============================================================
# GATE 4: SECTOR MEAN REVERSION ELIGIBILITY
# Hard gate — only trade sectors that reliably mean revert
# ============================================================
def check_sector_eligibility(symbol):
    """
    Technology and Financials in bear markets don't mean revert reliably.
    Consumer Staples, Healthcare, Energy do.
    """
    sector = INSTRUMENT_SECTOR.get(symbol, 'Unknown')

    # Check if sector is eligible
    eligible = any(s.lower() in sector.lower() for s in MEAN_REVERT_SECTORS)
    avoid    = any(s.lower() in sector.lower() for s in AVOID_CONTRARIAN_SECTORS)

    # Check regime — in bear markets, financials/tech contrarian is dangerous
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json') as f:
            regime = json.load(f)
        regime_label = regime.get('regime_label', 'NEUTRAL')

        if avoid and regime_label in ['HOSTILE', 'BLOCKED', 'CAUTIOUS']:
            return False, f"BLOCKED — {sector} contrarian in {regime_label} regime is high risk"
        elif avoid:
            return True, f"CAUTION — {sector} contrarian allowed in favourable regime only"
    except Exception as _e:
        log_error(f"Silent failure in apex-contrarian-gates.py: {_e}")

    if eligible:
        return True, f"CLEAR — {sector} reliably mean reverts"
    elif avoid:
        return False, f"BLOCKED — {sector} does not reliably mean revert in bear markets"
    else:
        return True, f"NEUTRAL — {sector} sector, proceed with caution"

# ============================================================
# GATE 5: STAGED ENTRY CALCULATION
# Returns initial and add-on position sizes
# ============================================================
def calculate_staged_entry(signal):
    """
    Splits contrarian entry into two stages:
    Stage 1: 50% of normal size at current signal
    Stage 2: Remaining 50% after 5 days if price stabilises

    Uses wider stop (3x ATR) with smaller initial size.
    Same total risk, better trade survival.
    """
    entry    = float(signal.get('entry', 0))
    atr      = float(signal.get('atr', entry * 0.02))
    qty      = float(signal.get('quantity', 1))

    # Stage 1: 50% initial
    stage1_qty  = round(qty * 0.5, 2)
    stage1_stop = round(entry - atr * 3.0, 2)  # Wider: 3x ATR
    stage1_risk = round(stage1_qty * (entry - stage1_stop), 2)

    # Stage 2: Add-on after stabilisation
    stage2_qty     = round(qty * 0.5, 2)
    stage2_trigger = round(entry * 1.02, 2)  # Price 2% above entry
    stage2_max     = round(entry * 1.04, 2)  # Max 4% above entry — don't chase
    stage2_stop    = round(entry - atr * 2.0, 2)  # Tighten stop on add-on
    stage2_risk    = round(stage2_qty * (entry - stage2_stop), 2)

    total_risk = round(stage1_risk + stage2_risk, 2)

    return {
        'stage1': {
            'qty':      stage1_qty,
            'stop':     stage1_stop,
            'stop_pct': round((entry - stage1_stop) / entry * 100, 2),
            'risk':     stage1_risk,
            'note':     f"Initial entry — {stage1_qty} shares @ £{entry} stop £{stage1_stop} (3x ATR)"
        },
        'stage2': {
            'qty':         stage2_qty,
            'trigger':     stage2_trigger,
            'max_price':   stage2_max,
            'stop':        stage2_stop,
            'risk':        stage2_risk,
            'days_to_wait':5,
            'note':        f"Add-on after 5 days if £{stage2_trigger} < price < £{stage2_max} — {stage2_qty} shares"
        },
        'total_risk':  total_risk,
        'vs_original': f"Original: {qty} @ 1.5x ATR | Staged: {stage1_qty}+{stage2_qty} @ 3x/2x ATR"
    }

# ============================================================
# MAIN — Run all gates for a signal
# ============================================================
def run_gates(signal):
    """
    Run all contrarian quality gates for a given signal.
    Returns: pass/fail, gate results, staged entry plan.
    """
    symbol = signal.get('name', '').upper()
    yahoo  = YAHOO_MAP.get(symbol, signal.get('ticker', symbol))

    print(f"\n  Running contrarian gates for {symbol}...")

    results = {}
    blocks  = []
    cautions= []

    # Gate 1: Earnings trajectory
    traj_pass, traj_note = check_earnings_trajectory(symbol)
    results['earnings_trajectory'] = {'pass': traj_pass, 'note': traj_note}
    if not traj_pass:
        blocks.append(f"Gate 1 FAIL: {traj_note}")
    elif 'CAUTION' in traj_note:
        cautions.append(f"Gate 1: {traj_note}")
    print(f"    Gate 1 (Earnings trajectory): {'✅' if traj_pass else '❌'} {traj_note[:60]}")

    # Gate 2: Catalyst
    has_cat, catalysts, cat_warnings = check_catalyst(symbol, yahoo)
    results['catalyst'] = {'pass': has_cat, 'catalysts': catalysts, 'warnings': cat_warnings}
    if not has_cat:
        cautions.append("Gate 2: No catalyst identified — risk of continued decline")
    for c in catalysts:
        print(f"    Gate 2 (Catalyst): ✅ {c[:70]}")
    for w in cat_warnings:
        print(f"    Gate 2 (Catalyst): ⚠️  {w[:70]}")

    # Gate 3: Fundamental floor
    floor_pass, floor_passed, floor_failed = check_fundamental_floor(symbol)
    results['fundamental_floor'] = {
        'pass': floor_pass,
        'passed': floor_passed,
        'failed': floor_failed
    }
    if not floor_pass:
        blocks.append(f"Gate 3 FAIL: Fundamental floor not confirmed")
    for p in floor_passed[:2]:
        print(f"    Gate 3 (Fund floor): ✅ {p[:70]}")
    for f in floor_failed[:2]:
        print(f"    Gate 3 (Fund floor): ❌ {f[:70]}")

    # Gate 4: Sector eligibility
    sect_pass, sect_note = check_sector_eligibility(symbol)
    results['sector_eligibility'] = {'pass': sect_pass, 'note': sect_note}
    if not sect_pass:
        blocks.append(f"Gate 4 FAIL: {sect_note}")
    print(f"    Gate 4 (Sector): {'✅' if sect_pass else '❌'} {sect_note[:70]}")

    # Gate 5: Staged entry
    staged = calculate_staged_entry(signal)
    results['staged_entry'] = staged
    print(f"    Gate 5 (Staged entry):")
    print(f"      Stage 1: {staged['stage1']['qty']} shares @ stop £{staged['stage1']['stop']} (3x ATR)")
    print(f"      Stage 2: {staged['stage2']['qty']} shares after {staged['stage2']['days_to_wait']} days")
    print(f"      Total risk: £{staged['total_risk']} (same as before, wider stops)")

    # Overall verdict
    hard_blocks  = len(blocks)
    soft_cautions= len(cautions)
    overall_pass = hard_blocks == 0

    print(f"\n    {'✅ GATES PASSED' if overall_pass else '❌ GATES FAILED'}")
    if blocks:
        for b in blocks:
            print(f"    🚫 {b}")
    if cautions:
        for c in cautions:
            print(f"    ⚠️  {c}")

    return {
        'symbol':       symbol,
        'overall_pass': overall_pass,
        'blocks':       blocks,
        'cautions':     cautions,
        'gates':        results,
        'staged_entry': staged,
    }

def check_signal(signal):
    """Entry point called by decision engine."""
    try:
        result = run_gates(signal)
        # Save
        try:
            with open(GATES_FILE) as f:
                existing = json.load(f)
        except:
            existing = {}
        existing[signal.get('name','?')] = result
        atomic_write(GATES_FILE, existing)
        return result
    except Exception as e:
        print(f"  Gate check error: {e}")
        return {'overall_pass': True, 'blocks': [], 'cautions': [], 'staged_entry': {}}

if __name__ == '__main__':
    import sys
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else 'ULVR'

    # Build a test signal
    yahoo = YAHOO_MAP.get(symbol, symbol)
    try:
        t     = yf.Ticker(yahoo)
        hist  = t.history(period="1mo")
        price = float(hist['Close'].iloc[-1])
        if yahoo.endswith('.L') and price > 100:
            price /= 100
    except:
        price = 100.0

    test_signal = {
        'name':        symbol,
        'ticker':      symbol,
        'entry':       price,
        'stop':        round(price * 0.94, 2),
        'quantity':    1.0,
        'atr':         round(price * 0.02, 4),
        'signal_type': 'CONTRARIAN',
    }

    result = run_gates(test_signal)
    print(f"\n  Overall: {'PASS' if result['overall_pass'] else 'FAIL'}")
