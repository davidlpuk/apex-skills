#!/usr/bin/env python3
"""
Apex Position Sizer
calculate_final_position() — Kelly + regime + drawdown + conviction sizing.
"""
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import log_error, log_warning, log_info, get_portfolio_value, get_free_cash
except ImportError:
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m): print(f'INFO: {m}')
    def get_portfolio_value(): return None
    def get_free_cash(): return None

_SCRIPTS = '/home/ubuntu/.picoclaw/scripts'


def calculate_final_position(signal, intel):
    entry = float(signal.get('entry', signal.get('price', 0)))
    stop  = float(signal.get('stop', entry * 0.94))

    if entry <= 0 or stop <= 0:
        return 1, 50

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 1, entry

    # Continuous regime scaling
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("rs", f"{_SCRIPTS}/apex-regime-scaling.py")
        _rs   = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_rs)
        regime_scale = _rs.get_scale_for_signal(signal.get('signal_type','TREND'))
    except Exception:
        regime_scale = 0.5

    portfolio_value = get_portfolio_value() or 5000
    risk_pct        = 0.01
    base_risk       = round(portfolio_value * risk_pct * regime_scale, 2)
    base_risk       = max(5.0, min(portfolio_value * 0.015, base_risk))

    score      = signal.get('adjusted_score', signal.get('total_score', 7))
    max_score  = 12
    conviction = score / max_score

    if signal.get('signal_type') == 'CONTRARIAN':
        conviction *= 0.8
    elif signal.get('signal_type') == 'EARNINGS_DRIFT':
        conviction *= 1.1
    elif signal.get('signal_type') == 'DIVIDEND_CAPTURE':
        conviction *= 0.9

    risk_amount = base_risk * conviction * intel['size_multiplier']
    risk_amount = max(5.0, min(portfolio_value * 0.015, round(risk_amount, 2)))

    # Kelly Criterion overlay — try v2 (continuous Kelly) first, fall back to thorp
    _kelly = None
    try:
        import importlib.util as _ilu_k2
        _spec_k2 = _ilu_k2.spec_from_file_location(
            "kelly_v2", f"{_SCRIPTS}/apex-kelly-v2.py")
        _kv2 = _ilu_k2.module_from_spec(_spec_k2)
        _spec_k2.loader.exec_module(_kv2)
        _kelly = _kv2.calculate_optimal_size_v2(signal, portfolio_value)
        if _kelly:
            log_info(f"Kelly v2 ({_kelly.get('stats_source','?')}): "
                     f"f*={_kelly.get('kelly_continuous',0):.3f}, "
                     f"adj={_kelly.get('kelly_adjusted_pct',0):.1f}%")
    except Exception as _ke2:
        log_warning(f"Kelly v2 failed, falling back to thorp-test: {_ke2}")

    if _kelly is None:
        try:
            import importlib.util as _ilu_k
            _spec_k = _ilu_k.spec_from_file_location(
                "thorp", f"{_SCRIPTS}/apex-thorp-test.py")
            _thorp = _ilu_k.module_from_spec(_spec_k)
            _spec_k.loader.exec_module(_thorp)
            _kelly = _thorp.calculate_optimal_size(signal, portfolio_value)
        except Exception as _ke:
            log_error(f"Kelly thorp fallback failed (non-fatal): {_ke}")

    if _kelly and _kelly.get('verdict') != 'ABORT':
        kelly_risk  = _kelly['recommended_risk']
        using_prior = _kelly['using_prior']

        if not using_prior:
            risk_amount = round(min(risk_amount, kelly_risk), 2)
            log_info(f"Kelly (real data, {_kelly['sample_count']} trades): "
                     f"£{kelly_risk} → using £{risk_amount}")
        else:
            kelly_soft_cap = round(kelly_risk * 1.2, 2)
            if risk_amount > kelly_soft_cap:
                risk_amount = kelly_soft_cap
                log_info(f"Kelly (prior): soft-capped risk at £{risk_amount}")

    elif _kelly and _kelly.get('verdict') == 'ABORT':
        risk_amount = max(5.0, portfolio_value * 0.002)
        log_warning(f"Kelly ABORT for {signal.get('name','?')}: "
                    f"{_kelly.get('verdict_reason','')} — sizing at minimum")

    qty      = round(risk_amount / risk_per_share, 2)
    notional = round(qty * entry, 2)

    # Cap notional at 8% of portfolio
    max_notional = portfolio_value * 0.08
    if notional > max_notional:
        qty      = round(max_notional / entry, 2)
        notional = round(qty * entry, 2)

    # Cash reserve enforcement — never commit >90% of free cash
    try:
        free_cash      = get_free_cash() or portfolio_value * 0.3
        cash_available = round(free_cash * 0.90, 2)
        if notional > cash_available and cash_available > 0:
            qty      = round(cash_available / entry, 2)
            notional = round(qty * entry, 2)
            log_info(f"Cash reserve cap: notional reduced to £{notional} "
                     f"(90% of £{free_cash:.2f} free cash)")
    except Exception as _ce:
        log_error(f"Cash reserve check failed (non-fatal): {_ce}")

    # Layer confidence penalty — if scoring layers failed, size down proportionally
    layer_conf = float(signal.get('layer_confidence', 1.0))
    if layer_conf < 0.6:
        qty      = round(max(1.0, qty * 0.25), 2)
        notional = round(qty * entry, 2)
        log_warning(f"Layer confidence {layer_conf:.0%} (<60%) — sizing at 25% minimum "
                    f"(failed: {signal.get('failed_layers', [])})")
    elif layer_conf < 0.9:
        qty      = round(qty * layer_conf, 2)
        notional = round(qty * entry, 2)
        log_info(f"Layer confidence {layer_conf:.0%} — size reduced proportionally")

    return qty, notional
