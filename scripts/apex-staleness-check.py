#!/usr/bin/env python3
import yfinance as yf
import json
import sys
from datetime import datetime, timezone

SIGNAL_FILE    = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
SCALING_FILE   = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'
WATCHLIST_YAHOO = {
    "VWRP": "VWRP.L", "VUAG": "VUAG.L", "VFEA": "VFEA.L",
    "IGWD": "IGWD.L", "HMWO": "HMWO.L", "IITU": "IITU.L",
    "IUFS": "IUFS.L", "IUHC": "IUHC.L", "IUES": "IUES.L",
    "IUCD": "IUCD.L", "SGLN": "SGLN.L", "SSLN": "SSLN.L",
    "ISF":  "ISF.L",  "CSPX": "CSPX.L", "EQQQ": "EQQQ.L",
    "HEAL": "HEAL.L", "INRG": "INRG.L", "WCLD": "WCLD.L",
    "VAPX": "VAPX.L", "VJPN": "VJPN.L", "VGOV": "VGOV.L",
    "VAGS": "VAGS.L", "HSBA": "HSBA.L", "SHEL": "SHEL.L",
    "AZN":  "AZN.L",  "ULVR": "ULVR.L", "GSK":  "GSK.L",
    "LLOY": "LLOY.L", "BP":   "BP.L",   "RIO":  "RIO.L",
    "BA":   "BA.L",   "REL":  "REL.L",  "BARC": "BARC.L",
    "NWG":  "NWG.L",  "PRU":  "PRU.L",  "NG":   "NG.L",
    "SSE":  "SSE.L",  "DGE":  "DGE.L",  "IMB":  "IMB.L",
    "BATS": "BATS.L", "EXPN": "EXPN.L", "CPG":  "CPG.L",
    "WPP":  "WPP.L",  "VOD":  "VOD.L",  "BT":   "BT-A.L",
    "AVIVA":"AV.L",   "AIR":  "AIR.PA", "LVMH": "MC.PA",
    "SAN":  "SAN.MC", "NOVN": "NOVN.SW","ROG":  "ROG.SW",
    "TTE":  "TTE.PA", "ASML": "ASML.AS","SIE":  "SIE.DE",
    "AAPL": "AAPL",   "MSFT": "MSFT",   "NVDA": "NVDA",
    "GOOGL":"GOOGL",  "AMZN": "AMZN",   "META": "META",
    "TSLA": "TSLA",   "CRM":  "CRM",    "ORCL": "ORCL",
    "AMD":  "AMD",    "INTC": "INTC",   "QCOM": "QCOM",
    "JPM":  "JPM",    "GS":   "GS",     "MS":   "MS",
    "BAC":  "BAC",    "BLK":  "BLK",    "AXP":  "AXP",
    "C":    "C",      "V":    "V",      "JNJ":  "JNJ",
    "PFE":  "PFE",    "MRK":  "MRK",    "UNH":  "UNH",
    "ABBV": "ABBV",   "TMO":  "TMO",    "DHR":  "DHR",
    "KO":   "KO",     "PEP":  "PEP",    "MCD":  "MCD",
    "WMT":  "WMT",    "PG":   "PG",     "XOM":  "XOM",
    "CVX":  "CVX",    "NOVO": "NVO",
}

def _get_regime_max_age():
    """
    Return (block_hours, warn_hours) based on current market regime.

    In hostile markets, signals go stale faster — a 3-hour-old signal in
    VIX-38 conditions is far more dangerous than the same signal in a calm
    uptrend. Scale the staleness window accordingly.

    Falls back to NEUTRAL thresholds if the scaling file is unavailable.
    """
    try:
        with open(SCALING_FILE) as f:
            scaling = json.load(f)
        scale = float(scaling.get('combined_scale', 0.5))
    except Exception:
        scale = 0.5  # NEUTRAL fallback
    if scale >= 0.8:
        return 6.0, 4.0   # FAVOURABLE  — block @6h, warn @4h
    elif scale >= 0.5:
        return 4.0, 2.0   # NEUTRAL     — block @4h, warn @2h  (original defaults)
    elif scale >= 0.2:
        return 3.0, 1.5   # CAUTIOUS    — block @3h, warn @1.5h
    else:
        return 2.0, 1.0   # HOSTILE/BLOCKED — block @2h, warn @1h


def fix_pence(price, currency):
    if currency == "GBX":
        return round(price / 100, 2)
    return price

def get_current_price(name, currency="USD"):
    yahoo_ticker = WATCHLIST_YAHOO.get(name)
    if not yahoo_ticker:
        return None
    try:
        hist = yf.Ticker(yahoo_ticker).history(period="1d", interval="5m")
        if hist.empty:
            hist = yf.Ticker(yahoo_ticker).history(period="2d")
        if hist.empty:
            return None
        price = float(hist['Close'].iloc[-1])
        return fix_pence(price, currency)
    except:
        return None

def check_staleness():
    try:
        with open(SIGNAL_FILE) as f:
            signal = json.load(f)
    except:
        print("ABORT|No signal file found")
        sys.exit(1)

    name          = signal.get('name', '?')
    ticker        = signal.get('t212_ticker', '?')
    signal_price  = float(signal.get('entry', 0))
    signal_score  = float(signal.get('score', 0))
    signal_stop   = float(signal.get('stop', 0))
    currency      = signal.get('currency', 'USD')
    generated_at  = signal.get('generated_at')

    blocks = []
    warnings = []
    now = datetime.now(timezone.utc)

    # Check 1 — signal age (regime-aware thresholds)
    max_age_hours, warn_age_hours = _get_regime_max_age()
    if generated_at:
        try:
            gen_dt  = datetime.fromisoformat(generated_at)
            age_hrs = (now - gen_dt).total_seconds() / 3600
            if age_hrs > max_age_hours:
                blocks.append(
                    f"Signal is {round(age_hrs,1)}h old — stale (max {max_age_hours:.0f}h in current regime)"
                )
            elif age_hrs > warn_age_hours:
                warnings.append(
                    f"Signal is {round(age_hrs,1)}h old — verify still valid (warn >{warn_age_hours:.1f}h)"
                )
        except:
            pass

    # Check 2 — current price vs signal price
    # Extract instrument name from t212 ticker for lookup
    instrument_name = None
    for k, v in WATCHLIST_YAHOO.items():
        if k.upper() in ticker.upper() or ticker.upper().startswith(k.upper()):
            instrument_name = k
            break

    current_price = None
    if instrument_name:
        # Try Alpaca first for US stocks, fall back to yfinance
        try:
            sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
            import apex_price_feed as pf
            live_price, _, source = pf.get_live_price(instrument_name)
            if live_price:
                current_price = live_price
                print(f"Live price from {source}: {current_price}")
            else:
                current_price = get_current_price(instrument_name, currency)
        except:
            current_price = get_current_price(instrument_name, currency)

    if current_price:
        price_change_pct = abs(current_price - signal_price) / signal_price * 100
        price_change_dir = "↑" if current_price > signal_price else "↓"

        if price_change_pct > 2.0:
            blocks.append(f"Price moved {round(price_change_pct,1)}% {price_change_dir} since signal (£{signal_price} → £{current_price}) — too much slippage")
        elif price_change_pct > 1.0:
            warnings.append(f"Price moved {round(price_change_pct,1)}% {price_change_dir} since signal (£{signal_price} → £{current_price})")

        # Check 3 — price still above stop
        if current_price <= signal_stop:
            blocks.append(f"Current price £{current_price} is at or below stop £{signal_stop} — do not enter")

        # Check 4 — price hasn't gapped up too far (chasing)
        if current_price > signal_price * 1.02:
            blocks.append(f"Price has risen 2%+ above signal entry — chasing, skip this trade")
    else:
        warnings.append("Could not fetch live price — proceeding with caution")

    # Output result
    if blocks:
        reasons = " | ".join(blocks)
        print(f"ABORT|{name}|{reasons}")
        if current_price:
            print(f"DETAIL|Signal price: £{signal_price} | Current: £{current_price} | Stop: £{signal_stop}")
    elif warnings:
        warn_str = " | ".join(warnings)
        print(f"WARN|{name}|{warn_str}")
        if current_price:
            print(f"DETAIL|Signal: £{signal_price} | Now: £{current_price} | Change: {round(price_change_pct,1)}%")
        print(f"PROCEED|Signal still valid — executing with warnings")
    else:
        change_str = f"£{signal_price} → £{current_price} ({round(price_change_pct,1)}%)" if current_price else "price unchanged"
        print(f"PROCEED|{name}|Signal valid — {change_str}")

if __name__ == '__main__':
    check_staleness()
