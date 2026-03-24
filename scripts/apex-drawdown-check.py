#!/usr/bin/env python3
import json
from datetime import datetime, timezone
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, get_portfolio_value
except ImportError:
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def get_portfolio_value(): return None


DRAWDOWN_FILE = '/home/ubuntu/.picoclaw/logs/apex-drawdown.json'
PEAK_FILE     = '/home/ubuntu/.picoclaw/logs/apex-portfolio-peak.json'

def load_peak():
    try:
        with open(PEAK_FILE) as f:
            return json.load(f)
    except:
        return {"peak": get_portfolio_value() or 5000.0, "date": "2026-03-19"}

def save_peak(peak, date):
    with open(PEAK_FILE, 'w') as f:
        json.dump({"peak": peak, "date": date}, f, indent=2)

def calculate_drawdown():
    current = get_portfolio_value()
    if not current:
        return None

    peak_data = load_peak()
    peak      = peak_data.get('peak', get_portfolio_value() or 5000.0)
    today     = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Update peak if new high
    if current > peak:
        peak = current
        save_peak(peak, today)

    drawdown_pct  = round((peak - current) / peak * 100, 2)
    drawdown_amt  = round(peak - current, 2)

    # Position size multiplier based on drawdown
    if drawdown_pct <= 2:
        multiplier = 1.0
        status     = "NORMAL"
        note       = "Full position sizing"
    elif drawdown_pct <= 5:
        multiplier = 0.75
        status     = "CAUTION"
        note       = "Reduced to 75% — portfolio down 2-5%"
    elif drawdown_pct <= 10:
        multiplier = 0.5
        status     = "REDUCED"
        note       = "Reduced to 50% — portfolio down 5-10%"
    elif drawdown_pct <= 15:
        multiplier = 0.25
        status     = "MINIMAL"
        note       = "Reduced to 25% — portfolio down 10-15%"
    else:
        multiplier = 0.0
        status     = "HALT"
        note       = "Trading halted — portfolio down 15%+"

    result = {
        "current":       current,
        "peak":          peak,
        "drawdown_pct":  drawdown_pct,
        "drawdown_amt":  drawdown_amt,
        "multiplier":    multiplier,
        "status":        status,
        "note":          note,
        "timestamp":     today,
        "updated_at":    datetime.now(timezone.utc).isoformat(),
    }

    atomic_write(DRAWDOWN_FILE, result)

    return result

def get_size_multiplier():
    try:
        with open(DRAWDOWN_FILE) as f:
            d = json.load(f)
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        # Refresh if from a different day
        if d.get('timestamp') != today:
            result = calculate_drawdown()
            return result.get('multiplier', 1.0) if result else 1.0
        # Also refresh if more than 4 hours old within today
        updated_at = d.get('updated_at', '')
        if updated_at:
            try:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)).total_seconds() / 3600
                if age_h > 4:
                    result = calculate_drawdown()
                    return result.get('multiplier', 1.0) if result else d.get('multiplier', 1.0)
            except Exception:
                pass
        return d.get('multiplier', 1.0)
    except Exception:
        result = calculate_drawdown()
        return result.get('multiplier', 1.0) if result else 1.0

if __name__ == '__main__':
    result = calculate_drawdown()
    if result:
        print(f"\n📊 DRAWDOWN CHECK")
        print(f"  Current:    £{result['current']}")
        print(f"  Peak:       £{result['peak']}")
        print(f"  Drawdown:   {result['drawdown_pct']}% (£{result['drawdown_amt']})")
        print(f"  Status:     {result['status']}")
        print(f"  Multiplier: {result['multiplier']}x")
        print(f"  Note:       {result['note']}")
