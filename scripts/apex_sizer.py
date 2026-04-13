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

    # RSI-conviction boost for deep oversold CONTRARIAN entries.
    # Extreme RSI readings signal higher-conviction reversals, not lower.
    # Migrated here from apex-decision-engine.py (was shadowing this module).
    # RSI < 25: +0.10 | < 20: +0.20 | < 15: +0.30 | < 10: force full scale
    sig_rsi = float(signal.get('rsi', 50))
    if signal.get('signal_type') == 'CONTRARIAN' and 0 < sig_rsi < 25:
        if sig_rsi < 10:
            rsi_boost = 1.0 - regime_scale
        elif sig_rsi < 15:
            rsi_boost = 0.30
        elif sig_rsi < 20:
            rsi_boost = 0.20
        else:
            rsi_boost = 0.10
        regime_scale = min(1.0, regime_scale + rsi_boost)
        log_info(f"RSI conviction boost: +{rsi_boost:.2f} (RSI {sig_rsi:.1f}) → scale {regime_scale:.2f}")

    portfolio_value = get_portfolio_value() or 5000
    # 2026-04-07: lifted base risk 1% → 1.75% and cap 1.5% → 2.5% to fix cash drag.
    # Historical Kelly (64% WR, 1.35 R) suggests ~12% per trade as 1/4-Kelly; 1% was 1/50-Kelly.
    risk_pct        = 0.0175
    base_risk       = round(portfolio_value * risk_pct * regime_scale, 2)
    base_risk       = max(5.0, min(portfolio_value * 0.025, base_risk))

    score      = signal.get('adjusted_score', signal.get('total_score', 7))
    max_score  = 12
    conviction = score / max_score

    if signal.get('signal_type') == 'CONTRARIAN':
        conviction *= 0.8
    elif signal.get('signal_type') == 'EARNINGS_DRIFT':
        conviction *= 1.1
    elif signal.get('signal_type') == 'DIVIDEND_CAPTURE':
        conviction *= 0.9

    risk_amount = base_risk * conviction * intel.get('size_multiplier', 1.0)
    risk_amount = max(5.0, min(portfolio_value * 0.025, round(risk_amount, 2)))

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
        kelly_risk  = _kelly.get('recommended_risk', risk_amount)
        using_prior = _kelly.get('using_prior', True)

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
        log_warning(f"Kelly ABORT for {signal.get('name','?')}: "
                    f"{_kelly.get('verdict_reason','')} — no mathematical edge, blocking trade")
        return 0, 0  # Hard block: negative Kelly = no edge, don't trade at any size

    qty      = round(risk_amount / risk_per_share, 2)
    notional = round(qty * entry, 2)

    # Cap notional at 8% of portfolio
    max_notional = portfolio_value * 0.08
    if notional > max_notional:
        qty      = round(max_notional / entry, 2)
        notional = round(qty * entry, 2)

    # Cash reserve enforcement — never commit >90% of free cash
    try:
        raw_free_cash = get_free_cash()
        # Distinguish None (API failure → conservative fallback) from 0.0 (no cash → block)
        free_cash = raw_free_cash if raw_free_cash is not None else portfolio_value * 0.3
        cash_available = round(free_cash * 0.90, 2)
        if cash_available <= 0:
            log_warning(f"Cash reserve: zero available cash — blocking new trade")
            return 0, 0
        if notional > cash_available:
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

    # ── Correlation-based concentration check ────────────────────────────────
    # Detects factor clustering: same-sector portfolio passes count/notional
    # limits but moves as one. Falls back to sector-proxy when cache is absent.
    # Does NOT block the trade — reduces size to protect against correlated drawdown.
    try:
        import json as _j, os as _os
        from datetime import datetime as _dt, timezone as _tz
        CORR_FILE  = '/home/ubuntu/.picoclaw/logs/apex-pairwise-corr.json'
        new_ticker = signal.get('t212_ticker', signal.get('ticker', ''))
        # Use open_positions already gathered in intel — avoids redundant file read
        open_pos   = intel.get('open_positions', [])
        if not isinstance(open_pos, list):
            open_pos = []

        open_tickers = [p.get('t212_ticker', '') for p in open_pos
                        if p.get('status') in ('protected', 'unprotected', 'entry_placed')]

        if open_tickers and new_ticker:
            corr_cache = {}
            try:
                with open(CORR_FILE) as cf:
                    cc = _j.load(cf)
                cache_ts = cc.get('generated', '')
                if cache_ts:
                    age_h = (_dt.now(_tz.utc)
                             - _dt.fromisoformat(cache_ts).replace(tzinfo=_tz.utc)
                             ).total_seconds() / 3600
                    # Tighten TTL during vol spikes — correlations can jump in hours
                    ttl_h = 4.0 if float(intel.get('vix', 20)) > 25 else 48.0
                    if age_h < ttl_h:
                        corr_cache = cc.get('correlations', {})
            except (FileNotFoundError, Exception):
                pass

            max_corr        = 0.0
            max_corr_ticker = ''
            new_sector      = signal.get('sector', '')
            for ot in open_tickers:
                if not ot or ot == new_ticker:
                    continue
                c = corr_cache.get(f"{new_ticker}:{ot}", corr_cache.get(f"{ot}:{new_ticker}"))
                if c is not None:
                    c = float(c)
                else:
                    # Sector-proxy fallback: same sector ≈ 0.72 assumed correlation
                    ot_sector = next((p.get('sector', '') for p in open_pos
                                      if p.get('t212_ticker') == ot), '')
                    c = 0.72 if (new_sector and ot_sector and new_sector == ot_sector) else 0.0
                if c > max_corr:
                    max_corr, max_corr_ticker = c, ot

            if max_corr >= 0.85:
                qty      = round(qty * 0.50, 2)
                notional = round(qty * entry, 2)
                log_warning(f"High correlation [{new_ticker} vs {max_corr_ticker}: {max_corr:.2f}]"
                            f" — size reduced 50% to £{notional}")
            elif max_corr >= 0.70:
                qty      = round(qty * 0.75, 2)
                notional = round(qty * entry, 2)
                log_info(f"Moderate correlation [{new_ticker} vs {max_corr_ticker}: {max_corr:.2f}]"
                         f" — size reduced 25% to £{notional}")
    except Exception as corr_e:
        log_warning(f"Correlation check failed (non-blocking): {corr_e}")

    # Risk-parity OVERWEIGHT penalty — nudge toward inverse-vol balance
    try:
        import json as _j
        HEAT_FILE = '/home/ubuntu/.picoclaw/logs/apex-portfolio-heat.json'
        with open(HEAT_FILE) as hf:
            heat_data = _j.load(hf)
        rp = heat_data.get('risk_parity', {})
        if rp.get('status') == 'REBALANCE_SUGGESTED':
            signal_ticker = signal.get('t212_ticker', signal.get('ticker', ''))
            for dev in rp.get('deviations', []):
                if dev.get('ticker') == signal_ticker and dev.get('direction') == 'OVERWEIGHT':
                    qty      = round(qty * 0.75, 2)
                    notional = round(qty * entry, 2)
                    log_info(f"Risk-parity: {signal_ticker} overweight by "
                             f"{dev.get('deviation_pct', 0):.0f}% — sizing -25% to £{notional}")
                    break
    except (FileNotFoundError, Exception) as rp_e:
        if not isinstance(rp_e, FileNotFoundError):
            log_warning(f"Risk-parity check failed (non-blocking): {rp_e}")

    # Minimum viable notional — below this floor slippage/spread consumes the edge entirely.
    # This catches compounded haircuts (drawdown × circuit_breaker × regime × Kelly)
    # that reduce a position to <£100 where a 0.1% spread = 10bps cost on <£1 profit.
    # Returning (0, 0) signals BLOCK to the caller — better to skip than waste a trade.
    MIN_VIABLE_NOTIONAL = 100.0
    if 0 < notional < MIN_VIABLE_NOTIONAL:
        log_warning(
            f"Position below minimum viable notional: £{notional:.2f} < £{MIN_VIABLE_NOTIONAL} "
            f"— returning (0, 0) to block (slippage destroys edge at this size)"
        )
        return 0, 0

    return qty, notional
