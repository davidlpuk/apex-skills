#!/usr/bin/env python3
"""
Continuous Regime Scaling
Replaces binary VIX threshold with smooth continuous scaling.
No cliff edges — gradual adjustment as conditions change.
"""
import json
import math
from datetime import datetime, timezone
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


REGIME_FILE  = '/home/ubuntu/.picoclaw/logs/apex-regime.json'
SCALING_FILE = '/home/ubuntu/.picoclaw/logs/apex-regime-scaling.json'

def calculate_scaling():
    try:
        with open(REGIME_FILE) as f:
            regime = json.load(f)
    except:
        return default_scaling()

    vix     = float(regime.get('vix', 20))
    breadth = float(regime.get('breadth_pct', 50))

    # VIX scaling — continuous smooth curve
    # At VIX 15 → 1.0 (full size)
    # At VIX 25 → 0.5 (half size)
    # At VIX 35 → 0.0 (no new trades)
    # Formula: linear interpolation capped at 0-1
    if vix <= 15:
        vix_scale = 1.0
    elif vix >= 35:
        vix_scale = 0.0
    else:
        vix_scale = round(1.0 - (vix - 15) / 20, 3)

    # Breadth scaling — continuous
    # At breadth 70%+ → 1.0
    # At breadth 40%  → 0.5
    # At breadth 20%  → 0.0
    if breadth >= 70:
        breadth_scale = 1.0
    elif breadth <= 20:
        breadth_scale = 0.0
    else:
        breadth_scale = round((breadth - 20) / 50, 3)

    # Combined scaling — geometric mean
    # Neither factor alone dominates
    combined = round(math.sqrt(vix_scale * breadth_scale), 3)

    # Signal type adjustments
    # Contrarian signals work in low breadth — partial override
    trend_scale       = combined
    contrarian_scale  = round(min(1.0, combined + 0.3), 3)  # Contrarian gets +0.3 boost
    drift_scale       = combined
    dividend_scale    = round(min(1.0, combined + 0.2), 3)  # Dividend capture less sensitive
    # Inverse ETFs — INVERSE relationship with regime
    # The worse the market, the BETTER the inverse signal sizing
    inverse_scale     = round(min(1.0, max(0.0, 1.0 - combined + 0.2)), 3)

    # Regime label — based on combined scale
    if combined >= 0.8:
        regime_label = "FAVOURABLE"
    elif combined >= 0.5:
        regime_label = "NEUTRAL"
    elif combined >= 0.2:
        regime_label = "CAUTIOUS"
    elif combined > 0:
        regime_label = "HOSTILE"
    else:
        regime_label = "BLOCKED"

    result = {
        "timestamp":        datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        "vix":              vix,
        "breadth":          breadth,
        "vix_scale":        vix_scale,
        "breadth_scale":    breadth_scale,
        "combined_scale":   combined,
        "trend_scale":      trend_scale,
        "contrarian_scale": contrarian_scale,
        "drift_scale":      drift_scale,
        "dividend_scale":   dividend_scale,
        "inverse_scale":    inverse_scale,
        "regime_label":     regime_label,
        "old_binary":       "BLOCKED" if vix >= 25 or breadth <= 40 else "CLEAR",
        "improvement":      "Continuous scaling vs binary block"
    }

    # Breadth thrust override
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "bt", "/home/ubuntu/.picoclaw/scripts/apex-breadth-thrust.py")
        _bt = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_bt)
        new_combined, override_reason = _bt.get_regime_override(combined)
        if new_combined != combined:
            result['override']        = override_reason
            result['combined_scale']  = new_combined
            result['trend_scale']     = new_combined
            print(f"  ⚡ Regime override: {override_reason} → {round(new_combined*100)}%")
    except Exception as _e:
        log_error(f"Silent failure in apex-regime-scaling.py: {_e}")

    atomic_write(SCALING_FILE, result)

    return result

def default_scaling():
    return {
        "combined_scale":   0.5,
        "trend_scale":      0.5,
        "contrarian_scale": 0.8,
        "dividend_scale":   0.7,
        "regime_label":     "NEUTRAL",
        "vix":              20,
        "breadth":          50
    }

def get_scale_for_signal(signal_type):
    try:
        with open(SCALING_FILE) as f:
            data = json.load(f)
        if signal_type == 'TREND':
            return data.get('trend_scale', 0.5)
        elif signal_type == 'CONTRARIAN':
            return data.get('contrarian_scale', 0.8)
        elif signal_type == 'EARNINGS_DRIFT':
            return data.get('drift_scale', 0.5)
        elif signal_type == 'DIVIDEND_CAPTURE':
            return data.get('dividend_scale', 0.7)
        return data.get('combined_scale', 0.5)
    except:
        return 0.5

def display(result):
    label = result['regime_label']
    icon  = {"FAVOURABLE":"🟢","NEUTRAL":"🟡","CAUTIOUS":"🟠","HOSTILE":"🔴","BLOCKED":"⛔"}.get(label,"⚪")

    print(f"\n=== CONTINUOUS REGIME SCALING ===")
    print(f"  VIX:     {result['vix']} → scale {result['vix_scale']:.2f}")
    print(f"  Breadth: {result['breadth']}% → scale {result['breadth_scale']:.2f}")
    print(f"  Combined: {result['combined_scale']:.2f} → {icon} {label}")
    print(f"")
    print(f"  Signal type scales:")
    print(f"    Trend:      {result['trend_scale']:.2f} ({round(result['trend_scale']*100)}% of full size)")
    print(f"    Contrarian: {result['contrarian_scale']:.2f} ({round(result['contrarian_scale']*100)}% of full size)")
    print(f"    Drift:      {result['drift_scale']:.2f} ({round(result['drift_scale']*100)}% of full size)")
    print(f"    Dividend:   {result['dividend_scale']:.2f} ({round(result['dividend_scale']*100)}% of full size)")
    print(f"")
    print(f"  Old binary system said: {result['old_binary']}")
    print(f"  New continuous system:  {label} at {round(result['combined_scale']*100)}% capacity")

if __name__ == '__main__':
    result = calculate_scaling()
    display(result)
