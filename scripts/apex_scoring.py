#!/usr/bin/env python3
"""
Apex Signal Scoring
18-layer intelligence scoring for trade signals.

Key design decisions:
- _MODULE_CACHE is module-level (not inside run()) — fixes silent NameError bug
  that was causing RS and MTF layers to silently fail on every signal.
- load_module() is the public entry point for lazy module loading.
- All scoring logic is verbatim from apex-decision-engine.py layers 2+3.
"""
import json
import re
import sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import safe_read, log_error, log_warning, log_info
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def log_info(m): print(f'INFO: {m}')

_SCRIPTS = '/home/ubuntu/.picoclaw/scripts'
_LOGS    = '/home/ubuntu/.picoclaw/logs'
QUALITY_FILE = f'{_SCRIPTS}/apex-quality-universe.json'

_BT_INSIGHTS_V2 = f'{_LOGS}/apex-backtest-v2-insights.json'

# Cached layer weights — refreshed once per process lifetime
# Structure: {'RS': 1.2, 'MTF': 1.0, 'FRED': 0.5, 'SENTIMENT': 0.75}
_LAYER_WEIGHT_CACHE = {}
_LAYER_WEIGHT_LOADED = False


_LEARNED_WEIGHTS_FILE = f'{_LOGS}/apex-learned-weights.json'

def _load_layer_weights() -> dict:
    """
    Load adaptive layer weights — prefers Bayesian learned weights over static
    backtest ablation step-function.

    Priority:
    1. apex-learned-weights.json (Bayesian, continuous [0.3–1.5]) — if present
       and n_signals_matched >= 10
    2. apex-backtest-v2-insights.json (4-level step function) — fallback

    Weight mapping for fallback (from layers_impact in insights):
        significant=True  AND lift >= 2.0%  → 1.2  (amplify: strongly validated)
        significant=True  AND lift >= 0.5%  → 1.0  (neutral: validated)
        significant=False AND lift >= 0.5%  → 0.75 (reduce: marginal significance)
        otherwise                           → 0.5  (halve: not statistically validated)

    Only layers present in the ablation study are weighted; all others use 1.0.
    Returns {} if insights file unavailable — caller treats missing key as weight=1.0.
    """
    global _LAYER_WEIGHT_CACHE, _LAYER_WEIGHT_LOADED
    if _LAYER_WEIGHT_LOADED:
        return _LAYER_WEIGHT_CACHE

    # ── Primary: Bayesian learned weights ──────────────────────────
    try:
        learned = safe_read(_LEARNED_WEIGHTS_FILE, {})
        if learned and learned.get('n_signals_matched', 0) >= 10:
            layers = learned.get('layers', {})
            weights = {k: v.get('weight', 1.0) for k, v in layers.items()}
            if weights:
                log_info(f"Learned weights loaded ({learned['n_signals_matched']} matched): "
                         f"{', '.join(f'{k}={v:.2f}' for k,v in sorted(weights.items()))}")
                _LAYER_WEIGHT_CACHE  = weights
                _LAYER_WEIGHT_LOADED = True
                return weights
    except Exception as e:
        log_warning(f"Learned weights load failed (falling back): {e}")

    # ── Fallback: backtest v2 OOS ablation step-function ───────────
    try:
        bt     = safe_read(_BT_INSIGHTS_V2, {})
        layers = bt.get('layers_impact', {})
        weights = {}
        for layer_name, data in layers.items():
            lift_str = str(data.get('oos_lift', '0%'))
            try:
                lift = abs(float(lift_str.replace('%', '').replace('+', '').strip()))
            except ValueError:
                lift = 0.0
            sig = bool(data.get('significant', False))

            if sig and lift >= 2.0:
                weights[layer_name.upper()] = 1.2
            elif sig and lift >= 0.5:
                weights[layer_name.upper()] = 1.0
            elif not sig and lift >= 0.5:
                weights[layer_name.upper()] = 0.75
            else:
                weights[layer_name.upper()] = 0.5

        if weights:
            log_info(f"Layer weights loaded from v2 insights: "
                     f"{', '.join(f'{k}={v}' for k,v in sorted(weights.items()))}")

        _LAYER_WEIGHT_CACHE  = weights
        _LAYER_WEIGHT_LOADED = True
        return weights

    except Exception as e:
        log_error(f"_load_layer_weights failed (non-fatal): {e}")
        _LAYER_WEIGHT_LOADED = True
        return {}

# ── Redundancy discount helpers ──────────────────────────────────────────────
# Maps the label prefix used in adjustment strings → canonical layer name.
# Must stay in sync with apex-layer-audit.py _LAYER_ALIAS.
_REDUND_ALIAS = {
    'macro':          'MACRO',
    'fred':           'FRED',
    'breadth':        'BREADTH',
    'breadth thrust': 'BREADTH',
    'backtest':       'BACKTEST',
    'sector':         'SECTOR',
    'fundamentals':   'FUND',
    'fundamentals5':  'FUND',
    'fundamental':    'FUND',
    'rs':             'RS',
    'mtf':            'MTF',
    'insider':        'INSIDER',
    'sentiment':      'SENT',
    'options':        'OPTIONS',
    'geo':            'GEO',
    'diverge':        'DIVERGE',
    'revision':       'REVISION',
    'vol_accumulation': 'VOL',
}
_REDUND_ADJ_RE = re.compile(r'^([A-Za-z_\s]+?)\s*:\s*([+-]?\d+(?:\.\d+)?)', re.IGNORECASE)


def _parse_layer_contribs(adjustments: list) -> dict:
    """
    Parse the existing adjustment string list into {LAYER_NAME: cumulative_contribution}.

    Uses the same regex as apex-layer-audit.py so canonical layer names match
    the correlation pairs stored in apex-layer-audit.json.

    Multiple firings of the same layer (e.g. two MACRO lines) are summed.
    Non-numeric adjustments (staleness notes, caps) are silently skipped.
    """
    contribs = {}
    for adj in adjustments:
        m = _REDUND_ADJ_RE.match(adj.strip())
        if not m:
            continue
        raw_label = m.group(1).strip().lower()
        try:
            val = float(m.group(2))
        except ValueError:
            continue
        layer = _REDUND_ALIAS.get(raw_label, raw_label.upper().replace(' ', '_'))
        contribs[layer] = contribs.get(layer, 0) + val
    return contribs


def _apply_redundancy_discount(adjustments: list) -> tuple:
    """
    Reduce score inflation when highly-correlated layers co-fire in the same direction.

    Mechanism:
      1. Load pairwise layer correlations from apex-layer-audit.json
         (refreshed by running apex-layer-audit.py — no live API calls here).
      2. For each high-correlation pair (|r| >= 0.70) where both fired:
         - Same direction  → redundant. Discount smaller contribution by |r|.
           e.g. BREADTH=-1, FRED=-1, r=+1.0 → discount = 1.0, delta = +1.0
           (only -1 net instead of -2)
         - Opposite direction → genuinely conflicting info, no discount.
      3. Returns (total_delta_float, list_of_explanation_strings).
         delta is added to total_score by the caller before the adjustment cap.

    Falls back to (0, []) silently when audit file is absent or has no pairs,
    so the scorer continues working even if the audit has never been run.
    """
    audit = safe_read(f'{_LOGS}/apex-layer-audit.json', {})
    pairs = audit.get('high_corr_pairs', [])
    if not pairs:
        return 0.0, []

    contribs = _parse_layer_contribs(adjustments)
    if not contribs:
        return 0.0, []

    total_delta     = 0.0
    notes           = []
    processed_pairs = set()

    for pair in pairs:
        la  = str(pair.get('la', '')).upper()
        lb  = str(pair.get('lb', '')).upper()
        r   = float(pair.get('r', 0))

        if abs(r) < 0.70:
            continue

        key = frozenset([la, lb])
        if key in processed_pairs:
            continue

        val_a = contribs.get(la, 0)
        val_b = contribs.get(lb, 0)

        if val_a == 0 or val_b == 0:
            continue  # at least one layer didn't fire on this signal

        # Discount only when the co-firing is CONSISTENT with the historical correlation.
        #
        # Consistent = sign(val_a × val_b) == sign(r):
        #   r > 0, both same direction  → they always do this → redundant → discount ✓
        #   r < 0, opposite directions  → they always do this → redundant → discount ✓
        #   r > 0, opposite directions  → unusual divergence  → genuine conflict → no discount
        #   r < 0, same direction       → unusual agreement   → stronger signal  → no discount
        #
        # Example: GEO=+2 and SENT=+1 when r(GEO,SENT)=-1.0 → both positive despite always
        # going opposite = rare agreement = genuine signal, not redundancy.
        if (val_a * val_b * r) <= 0:
            continue  # firing inconsistent with historical pattern — keep both at full weight

        # Discount the smaller-magnitude contributor proportionally to |r|.
        # r=1.0 → 100% of smaller removed (fully redundant)
        # r=0.7 → 70% of smaller removed
        if abs(val_a) <= abs(val_b):
            redundant_layer, redundant_val = la, val_a
        else:
            redundant_layer, redundant_val = lb, val_b

        discount_amount = redundant_val * abs(r)   # same sign as original contribution
        score_delta     = -discount_amount          # undo the double-count

        total_delta += score_delta
        processed_pairs.add(key)
        notes.append(
            f"Redundancy discount: {score_delta:+.2f} "
            f"({la}↔{lb} r={r:+.2f}, {redundant_layer} ×{round(1-abs(r),2)} marginal)"
        )

    return round(total_delta, 3), notes


# ── Module cache — module-level so scoring layers can access it ───────────────
# Previously this was a local inside run(), causing NameError inside
# score_signal_with_intelligence() for RS and MTF layers (silently caught).
_MODULE_CACHE = {}


def load_module(alias, filepath):
    """Load and cache a Python module by alias. Thread-safe for single process."""
    if alias in _MODULE_CACHE:
        return _MODULE_CACHE[alias]
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(alias, filepath)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _MODULE_CACHE[alias] = _mod
        return _mod
    except Exception as _e:
        log_error(f"Module load failed: {alias} — {_e}")
        return None


# ── LAYER 2: Sector / Geo helpers ─────────────────────────────────────────────

SECTOR_MAP = {
    "Energy":     ["XOM","CVX","SHEL","BP","TTE","IUES","NG_EQ","SSE_EQ","INRG"],
    "Technology": ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","AMD","CRM","ORCL","QCOM","IITU"],
    "Financials": ["JPM","GS_EQ","MS_EQ","BAC","BLK","V_US","AXP","HSBA","BARC","NWG","IUFS"],
    "Healthcare": ["JNJ","PFE","MRK","UNH","ABBV","AZN","GSK","TMO","DHR","IUHC","NOVO"],
    "Consumer":   ["KO","PEP","MCD","WMT","PG","DGE","ULVR","CPG","IMB","BATS","IUCD"],
}


def get_instrument_sector(name):
    name_upper = name.upper().strip()
    for sector, instruments in SECTOR_MAP.items():
        for inst in instruments:
            inst_upper = inst.upper().strip()
            if name_upper == inst_upper or name_upper == inst_upper.replace("_EQ","").replace("_US",""):
                return sector
            if len(inst_upper) <= 5 and inst_upper in name_upper.split():
                return sector
    return None


def get_sector_boost(name, intel):
    sector = get_instrument_sector(name)
    if not sector:
        return 0, "Unknown sector"

    sector_score   = intel['sector_scores'].get(sector, 5)
    sector_breadth = intel['sector_breadth'].get(sector, {})
    breadth_200    = sector_breadth.get('breadth_200', 50)
    health         = sector_breadth.get('health', 'NEUTRAL')

    boost   = 0
    reasons = []

    if sector in intel['leading_sectors']:
        boost += 2
        reasons.append(f"{sector} is leading sector ({sector_score}/10 rotation score)")
    elif sector in intel['lagging_sectors']:
        boost -= 1
        reasons.append(f"{sector} is lagging sector ({sector_score}/10 rotation score)")

    if breadth_200 >= 60:
        boost += 1
        reasons.append(f"{sector} breadth strong ({breadth_200}% above 200 EMA)")
    elif breadth_200 <= 25:
        boost -= 1
        reasons.append(f"{sector} breadth weak ({breadth_200}% above 200 EMA)")

    if health == 'BEARISH' and boost <= 0:
        boost -= 1

    return boost, " | ".join(reasons) if reasons else f"{sector} sector neutral"


def get_geo_adjustment(name, intel):
    if intel['geo_status'] != 'ALERT':
        return 0, []

    try:
        with open(QUALITY_FILE) as f:
            quality_db = json.load(f)
        geo_map  = quality_db.get('geo_event_map', {})
        favoured = geo_map.get('iran_war', {}).get('favour', [])
        avoided  = geo_map.get('iran_war', {}).get('avoid', [])
    except Exception:
        favoured = ['XOM','CVX','SHEL','BP','TTE','IUES']
        avoided  = []

    name_upper  = name.upper().strip()
    is_favoured = any(
        name_upper == f.upper() or
        name_upper in f.upper().split('_') or
        f.upper() in name_upper.split('_')
        for f in favoured
    )
    is_avoided = any(name_upper == a.upper() for a in avoided)

    if is_favoured:
        return 2, [f"Geo-reversal: {name} benefits from current conflict"]
    elif is_avoided:
        return -2, [f"Geo risk: {name} hurt by current conflict"]
    return 0, []


# ── Ticker normalisation ──────────────────────────────────────────────────────
# Canonical Yahoo → T212 map. Signals from market-data and contrarian-scan carry
# a Yahoo ticker in 'ticker' but no 't212_ticker'. Resolving once here means all
# 18 scoring layers can rely on signal['yahoo_ticker'] and signal['t212_ticker']
# without individual cascading .get() chains that silently return the wrong format.

_YAHOO_TO_T212 = {
    'AAPL':'AAPL_US_EQ',  'MSFT':'MSFT_US_EQ',  'NVDA':'NVDA_US_EQ',
    'GOOGL':'GOOGL_US_EQ','AMZN':'AMZN_US_EQ',  'META':'META_US_EQ',
    'TSLA':'TSLA_US_EQ',  'V':'V_US_EQ',         'XOM':'XOM_US_EQ',
    'CVX':'CVX_US_EQ',    'JPM':'JPM_US_EQ',     'GS':'GS_US_EQ',
    'ABBV':'ABBV_US_EQ',  'JNJ':'JNJ_US_EQ',     'UNH':'UNH_US_EQ',
    'NFLX':'NFLX_US_EQ',  'HOOD':'HOOD_US_EQ',   'PLTR':'PLTR_US_EQ',
    'BAC':'BAC_US_EQ',    'BLK':'BLK_US_EQ',     'KO':'KO_US_EQ',
    'PEP':'PEP_US_EQ',    'PG':'PG_US_EQ',       'WMT':'WMT_US_EQ',
    'VUAG.L':'VUAGl_EQ',  'QQQS.L':'QQQSl_EQ',
    'AZN.L':'AZN_EQ',     'SHEL.L':'SHEL_EQ',    'HSBA.L':'HSBA_EQ',
    'GSK.L':'GSK_EQ',     'ULVR.L':'ULVR_EQ',
    'SQQQ':'SQQQ_EQ',     'SPXU':'SPXU_EQ',
}

def _resolve_tickers(signal):
    """
    Ensure signal has both 'yahoo_ticker' and 't212_ticker' set consistently.
    Mutates the signal dict in-place; safe to call multiple times (idempotent).
    """
    yahoo = signal.get('yahoo_ticker') or signal.get('ticker', '')
    t212  = signal.get('t212_ticker', '')

    # Derive T212 from Yahoo if missing
    if yahoo and not t212:
        t212 = _YAHOO_TO_T212.get(yahoo, '')

    # Derive Yahoo from T212 by stripping suffix if still missing
    if t212 and not yahoo:
        yahoo = t212.replace('_US_EQ', '').replace('_EQ', '').replace('l_EQ', '')

    signal['yahoo_ticker'] = yahoo
    signal['t212_ticker']  = t212


# ── LAYER 3: Full 18-layer signal scoring ────────────────────────────────────

def score_signal_with_intelligence(signal, intel):
    _resolve_tickers(signal)   # normalise ticker fields once before any layer reads them
    name        = signal.get('name', '')
    base_score  = float(signal.get('total_score', signal.get('contrarian_score', signal.get('score', 0))))
    signal_type = signal.get('signal_type', 'TREND')

    adjustments   = []
    failed_layers = []   # tracks which scoring layers raised exceptions
    total_score   = base_score
    _lw          = _load_layer_weights()   # {} if no insights file — all weights default to 1.0

    # Layer: Relative strength adjustment
    try:
        _rs_mod = _MODULE_CACHE.get('rs2') or load_module('rs2', f'{_SCRIPTS}/apex-relative-strength.py')
        if _rs_mod:
            _rs_adj, _rs_reason = _rs_mod.get_rs_adjustment(name, signal_type)
            if _rs_adj != 0:
                _rs_w   = _lw.get('RS', 1.0)
                _rs_adj = round(_rs_adj * _rs_w, 2)
                total_score += _rs_adj
                _w_tag = f" [w={_rs_w}]" if _rs_w != 1.0 else ""
                adjustments.append(f"RS: {_rs_adj:+.2g}{_w_tag} ({_rs_reason})")
    except Exception as _e:
        failed_layers.append('RS')
        log_error(f"RS adjustment failed for {name}: {_e}")

    # Layer: Multi-timeframe analysis adjustment
    try:
        _mtf_mod = _MODULE_CACHE.get('mtf') or load_module('mtf', f'{_SCRIPTS}/apex-multiframe.py')
        if _mtf_mod:
            _mtf_adj, _mtf_reason = _mtf_mod.get_adjustment_for_signal(name, signal_type)
            if _mtf_adj != 0:
                _mtf_w   = _lw.get('MTF', 1.0)
                _mtf_adj = round(_mtf_adj * _mtf_w, 2)
                total_score += _mtf_adj
                _w_tag = f" [w={_mtf_w}]" if _mtf_w != 1.0 else ""
                adjustments.append(f"MTF: {_mtf_adj:+.2g}{_w_tag} ({_mtf_reason[:60]})")
    except Exception as _e:
        failed_layers.append('MTF')
        log_error(f"MTF adjustment failed for {name}: {_e}")

    # Layer 14: Cross-asset macro confirmation
    try:
        import importlib.util as _ilu_mac
        _spec_mac = _ilu_mac.spec_from_file_location(
            "macro", f"{_SCRIPTS}/apex-macro-signals.py")
        _mac = _ilu_mac.module_from_spec(_spec_mac)
        _spec_mac.loader.exec_module(_mac)
        _mdata = safe_read(f'{_LOGS}/apex-macro-signals.json', {})
        # Staleness gate — skip macro layer if data is >12h old (same pattern as sentiment 24h gate)
        _mac_ts  = _mdata.get('timestamp', '')
        _mac_age = 0
        try:
            from datetime import datetime as _dt2, timezone as _tz2
            _mac_dt  = _dt2.strptime(_mac_ts, '%Y-%m-%d %H:%M UTC').replace(tzinfo=_tz2.utc)
            _mac_age = (_dt2.now(_tz2.utc) - _mac_dt).total_seconds() / 3600
            if _mac_age > 12:
                adjustments.append(f"MACRO: skipped — data {_mac_age:.0f}h old (max 12h)")
                raise Exception("stale")
        except ValueError:
            pass  # timestamp missing or unparseable — proceed anyway
        _md    = _mdata.get('macro_data', {})
        if _md:
            _yahoo_ticker  = signal.get('ticker', name)
            _YAHOO_TO_T212 = {
                'XOM':'XOM_US_EQ','CVX':'CVX_US_EQ',
                'AAPL':'AAPL_US_EQ','MSFT':'MSFT_US_EQ',
                'V':'V_US_EQ','JPM':'JPM_US_EQ',
                'ABBV':'ABBV_US_EQ','JNJ':'JNJ_US_EQ',
                'UNH':'UNH_US_EQ','AZN.L':'AZN_EQ',
                'GSK.L':'GSK_EQ','ULVR.L':'ULVR_EQ',
                'HSBA.L':'HSBA_EQ','SHEL.L':'SHEL_EQ',
                'VUAG.L':'VUAGl_EQ','QQQS.L':'QQQSl_EQ',
                'SQQQ':'SQQQ_EQ','SPXU':'SPXU_EQ',
            }
            _macro_ticker = _YAHOO_TO_T212.get(_yahoo_ticker, _yahoo_ticker)
            _macro_adj, _macro_reasons = _mac.get_macro_adjustment(
                _macro_ticker, signal_type, _md)
            if _macro_adj != 0:
                total_score += _macro_adj
                for _mr in _macro_reasons[:2]:
                    adjustments.append(f"MACRO: {'+' if _macro_adj > 0 else ''}{_macro_adj} ({_mr[:55]})")
    except Exception as _e:
        if str(_e) != 'stale':
            failed_layers.append('MACRO')
            log_error(f"Macro adjustment failed for {name}: {_e}")

    # Layer 14.5: Cross-asset divergence signal
    try:
        import importlib.util as _ilu_div
        _spec_div = _ilu_div.spec_from_file_location(
            "div", f"{_SCRIPTS}/apex-divergence-detector.py")
        _div = _ilu_div.module_from_spec(_spec_div)
        _spec_div.loader.exec_module(_div)
        _div_adj, _div_reasons = _div.get_divergence_adjustment(name, signal['t212_ticker'] or signal['yahoo_ticker'], signal_type)
        if _div_adj != 0:
            total_score += _div_adj
            adjustments.append(f"DIVERGE: {_div_adj:+.1f} ({_div_reasons[0][:55] if _div_reasons else ''})")
    except Exception as _e:
        failed_layers.append('DIVERGE')
        log_error(f"Divergence adjustment failed for {name}: {_e}")

    # Layer 15: EDGAR Insider Data
    try:
        import importlib.util as _ilu_ins
        _spec_ins = _ilu_ins.spec_from_file_location(
            "ins", f"{_SCRIPTS}/apex-insider-data.py")
        _ins = _ilu_ins.module_from_spec(_spec_ins)
        _spec_ins.loader.exec_module(_ins)
        _ins_adj, _ins_reasons = _ins.get_insider_adjustment(name, signal_type)
        if _ins_adj != 0:
            total_score += _ins_adj
            adjustments.append(f"INSIDER: +{_ins_adj} ({_ins_reasons[0][:55] if _ins_reasons else ''})")
    except Exception as _e:
        failed_layers.append('INSIDER')
        log_error(f"Insider adjustment failed for {name}: {_e}")

    # Layer 15.5: Earnings Revision Momentum
    try:
        import importlib.util as _ilu_rev
        _spec_rev = _ilu_rev.spec_from_file_location(
            "rev", f"{_SCRIPTS}/apex-earnings-revision.py")
        _rev = _ilu_rev.module_from_spec(_spec_rev)
        _spec_rev.loader.exec_module(_rev)
        _rev_adj, _rev_reasons = _rev.get_revision_momentum(name, signal['t212_ticker'] or signal['yahoo_ticker'], signal_type)
        if _rev_adj != 0:
            total_score += _rev_adj
            adjustments.append(f"REVISION: {_rev_adj:+.1f} ({_rev_reasons[0][:55] if _rev_reasons else ''})")
    except Exception as _e:
        failed_layers.append('REVISION')
        log_error(f"Earnings revision adjustment failed for {name}: {_e}")

    # Layer 16: FRED Macro Economic Signal
    try:
        import importlib.util as _ilu_fred
        _spec_fred = _ilu_fred.spec_from_file_location(
            "fred", f"{_SCRIPTS}/apex-fred-macro.py")
        _fred = _ilu_fred.module_from_spec(_spec_fred)
        _spec_fred.loader.exec_module(_fred)
        _fred_adj, _fred_reasons = _fred.get_fred_adjustment(signal_type)
        if _fred_adj != 0:
            _fred_w   = _lw.get('FRED', 1.0)
            _fred_adj = round(_fred_adj * _fred_w, 2)
            total_score += _fred_adj
            _w_tag = f" [w={_fred_w}]" if _fred_w != 1.0 else ""
            adjustments.append(f"FRED: {_fred_adj:+.2g}{_w_tag} ({_fred_reasons[0][:55] if _fred_reasons else ''})")
    except Exception as _e:
        failed_layers.append('FRED')
        log_error(f"FRED adjustment failed for {name}: {_e}")

    # Layer 17: Options Flow Signal
    try:
        import importlib.util as _ilu_opts
        _spec_opts = _ilu_opts.spec_from_file_location(
            "opts", f"{_SCRIPTS}/apex-options-flow.py")
        _opts = _ilu_opts.module_from_spec(_spec_opts)
        _spec_opts.loader.exec_module(_opts)
        _T212_TO_YAHOO_OPTS = {
            'AAPL_US_EQ':'AAPL', 'MSFT_US_EQ':'MSFT', 'NVDA_US_EQ':'NVDA',
            'AMZN_US_EQ':'AMZN', 'GOOGL_US_EQ':'GOOGL','META_US_EQ':'META',
            'TSLA_US_EQ':'TSLA', 'V_US_EQ':'V',        'XOM_US_EQ':'XOM',
            'CVX_US_EQ':'CVX',   'HOOD_US_EQ':'HOOD',  'PLTR_US_EQ':'PLTR',
            'NFLX_US_EQ':'NFLX',
        }
        _OPTS_UNIVERSE = {'AAPL','MSFT','NVDA','AMZN','GOOGL','META',
                          'TSLA','V','XOM','CVX','HOOD','PLTR','NFLX'}
        # Options universe uses Yahoo tickers — use yahoo_ticker directly (already normalised)
        _opts_ticker = signal['yahoo_ticker'] or signal['t212_ticker'].replace('_US_EQ','').replace('_EQ','')
        if _opts_ticker in _OPTS_UNIVERSE:
            _opts_adj, _opts_reasons = _opts.get_options_adjustment(_opts_ticker, signal_type)
            if _opts_adj != 0:
                total_score += _opts_adj
                adjustments.append(f"OPTIONS: {_opts_adj:+d} ({_opts_reasons[0][:55] if _opts_reasons else ''})")
    except Exception as _e:
        failed_layers.append('OPTIONS')
        log_error(f"Options flow adjustment failed for {name}: {_e}")

    # VOL_ACCUMULATION: institutional volume spike signal
    try:
        _vol_w   = _lw.get('VOL_ACCUMULATION', 1.0)
        _vol_adj = 0.0
        _vol_reason = ""
        _vr = signal.get('volume_ratio', signal.get('vol_ratio'))
        if _vr is not None:
            _vr = float(_vr)
            _trend = signal.get('trend', '')
            if signal_type == 'TREND':
                if _vr >= 2.0 and _trend == 'BULLISH':
                    _vol_adj    = 1.0
                    _vol_reason = f"vol {_vr}x avg — institutional buying ({_trend})"
                elif _vr >= 2.0 and _trend == 'BEARISH':
                    _vol_adj    = -1.0
                    _vol_reason = f"vol {_vr}x avg — distribution warning ({_trend})"
            elif signal_type == 'CONTRARIAN':
                if _vr >= 2.0:
                    _vol_adj    = 0.5
                    _vol_reason = f"vol {_vr}x avg — capitulation volume (reversal signal)"
        if _vol_adj:
            _vol_adj_weighted = round(_vol_adj * _vol_w, 2)
            total_score += _vol_adj_weighted
            _w_tag = f" [w={_vol_w}]" if _vol_w != 1.0 else ""
            adjustments.append(f"VOL_ACCUMULATION: {_vol_adj_weighted:+.2g}{_w_tag} ({_vol_reason})")
    except Exception as _e:
        failed_layers.append('VOL_ACCUMULATION')
        log_error(f"VOL_ACCUMULATION adjustment failed for {name}: {_e}")

    # Breadth thrust regime adjustment
    try:
        with open(f'{_LOGS}/apex-breadth-thrust.json') as _bt_f:
            _bt_data = json.load(_bt_f)
        _bt_signal = _bt_data.get('composite_signal', 0)
        if _bt_signal >= 3 and signal_type == 'TREND':
            total_score += 1
            adjustments.append("Breadth thrust: +1 (rare bull signal active)")
        elif _bt_signal <= -2 and signal_type == 'CONTRARIAN':
            total_score -= 1
            adjustments.append(f"Breadth: -1 (deterioration {_bt_data.get('divergence',{}).get('breadth_trend',0):+.1f}%)")
    except Exception as _e:
        failed_layers.append('BREADTH')
        log_error(f"Breadth thrust adjustment failed for {name}: {_e}")

    # Backtest instrument boost (v2 with OOS validation, falls back to v1)
    try:
        _bt_v2_path = f'{_LOGS}/apex-backtest-v2-insights.json'
        _bt_v1_path = f'{_LOGS}/apex-backtest-insights.json'
        import os as _os
        import time as _time
        # Fix 4: warn if backtest insights are stale (>7 days)
        if _os.path.exists(_bt_v2_path):
            _bt_age_days = (_time.time() - _os.path.getmtime(_bt_v2_path)) / 86400
            if _bt_age_days > 7:
                adjustments.append(
                    f"BACKTEST-WARN: insights {_bt_age_days:.0f}d old — re-run apex-backtest-v2.py")
        if _os.path.exists(_bt_v2_path):
            with open(_bt_v2_path) as _f:
                bt = json.load(_f)
            best  = bt.get('backtest_boost_instruments', bt.get('best_instruments', []))
            worst = bt.get('backtest_penalise_instruments', bt.get('worst_instruments', []))
        else:
            with open(_bt_v1_path) as _f:
                bt = json.load(_f)
            signal_type_key = 'trend_strategy' if signal_type == 'TREND' else 'contrarian_strategy'
            best  = bt.get(signal_type_key, {}).get('best_instruments', [])
            worst = bt.get(signal_type_key, {}).get('worst_instruments', [])
        if name in best:
            total_score += 1
            adjustments.append(f"Backtest: +1 (top performer for {signal_type})")
        elif name in worst:
            total_score -= 1
            adjustments.append(f"Backtest: -1 (poor performer for {signal_type})")
    except Exception as _e:
        failed_layers.append('BACKTEST')
        log_error(f"Backtest adjustment failed for {name}: {_e}")

    # Sector rotation boost — skip if data is stale (>24h)
    _sect_age    = intel.get('file_ages_hours', {}).get('sector_rotation', 0)
    _breadth_age = intel.get('file_ages_hours', {}).get('breadth', 0)
    if _sect_age > 24 or _breadth_age > 24:
        adjustments.append(
            f"SECTOR: skipped — sector_rotation {_sect_age:.0f}h old, breadth {_breadth_age:.0f}h old (max 24h)")
    else:
        sector_boost, sector_reason = get_sector_boost(name, intel)
        if sector_boost != 0:
            total_score += sector_boost
            adjustments.append(f"Sector: {'+' if sector_boost > 0 else ''}{sector_boost} ({sector_reason})")

    # Geo adjustment
    geo_boost, geo_reasons = get_geo_adjustment(name, intel)
    if geo_boost != 0:
        total_score += geo_boost
        adjustments.append(f"Geo: {'+' if geo_boost > 0 else ''}{geo_boost} ({', '.join(geo_reasons)})")

    # Geo-correlation cap — geo and sector boosts from same event must not double-count
    try:
        _geo_active = (intel.get('geo', {}).get('overall', 'CLEAR') == 'ALERT')
        if _geo_active and geo_boost > 0 and sector_boost > 0:
            _combined_geo_stack = geo_boost + sector_boost
            _geo_cap = 3
            if _combined_geo_stack > _geo_cap:
                _excess = _combined_geo_stack - _geo_cap
                total_score -= _excess
                adjustments.append(
                    f"Geo-correlation cap: -{_excess} "
                    f"(geo +{geo_boost} + sector +{sector_boost} capped at +{_geo_cap})"
                )
    except Exception:
        pass

    # VIX sensitivity penalty
    vix_corr = intel['position_vix_sensitivity'].get(
        next((p.get('t212_ticker','') for p in intel['open_positions'] if name in p.get('name','')), ''),
        -0.2
    )
    if vix_corr <= -0.6 and intel['vix'] >= 25:
        total_score -= 1
        adjustments.append(f"VIX sensitivity penalty: -1 (corr {vix_corr}, VIX {intel['vix']})")

    # Advanced fundamental signals (5-factor composite)
    try:
        with open(f'{_LOGS}/apex-fundamental-signals.json') as _ff:
            _fsig = json.load(_ff)
        _fdata      = _fsig.get('data', {}).get(name, {})
        _comp_score = _fdata.get('composite_score', 0)
        _composite  = _fdata.get('composite', 'NEUTRAL')
        _short      = _fdata.get('short_interest', {})
        _short_pct  = _short.get('short_pct_float', 0) if _short else 0

        if signal_type == 'CONTRARIAN':
            if _comp_score >= 2:
                total_score += 2
                adjustments.append(f"Fundamentals5: +2 ({_composite})")
            elif _comp_score == 1:
                total_score += 1
                adjustments.append(f"Fundamentals5: +1 ({_composite})")
            elif _comp_score <= -2:
                total_score -= 2
                adjustments.append(f"Fundamentals5: -2 ({_composite}) — avoid")
            elif _comp_score == -1:
                total_score -= 1
                adjustments.append(f"Fundamentals5: -1 ({_composite})")
            if _short_pct > 10:
                total_score += 1
                adjustments.append(f"Short squeeze: +1 ({round(_short_pct,1)}% short float)")
        elif signal_type == 'TREND':
            if _comp_score >= 3:
                total_score += 1
                adjustments.append(f"Fundamentals5: +1 ({_composite})")
            elif _comp_score <= -2:
                total_score -= 1
                adjustments.append(f"Fundamentals5: -1 ({_composite})")
            if _short_pct > 15:
                total_score -= 1
                adjustments.append(f"Short interest: -1 ({round(_short_pct,1)}% float short)")
    except Exception as _e:
        failed_layers.append('FUND5')
        log_error(f"Fundamentals5 adjustment failed for {name}: {_e}")

    # Fundamental data boost (legacy EV/EBITDA score)
    try:
        with open(f'{_LOGS}/apex-fundamentals.json') as _ff:
            _fund_db  = json.load(_ff)
        _fund_data    = _fund_db.get('data', {}).get(name, {})
        _fund_score   = _fund_data.get('fund_score', 5)
        _fund_class   = _fund_data.get('fund_class', 'NEUTRAL')
        _fund_reasons = _fund_data.get('reasons', [])

        if signal_type in ['CONTRARIAN', 'EARNINGS_DRIFT']:
            if _fund_score >= 9:
                total_score += 2
                adjustments.append(f"Fundamentals: +2 ({_fund_class} — {_fund_reasons[0] if _fund_reasons else ''})")
            elif _fund_score >= 7:
                total_score += 1
                adjustments.append(f"Fundamentals: +1 ({_fund_class})")
            elif _fund_score <= 3:
                total_score -= 2
                adjustments.append(f"Fundamentals: -2 ({_fund_class} — avoid value trap)")
            elif _fund_score <= 4:
                total_score -= 1
                adjustments.append(f"Fundamentals: -1 ({_fund_class})")
        elif signal_type == 'TREND':
            if _fund_score >= 9:
                total_score += 1
                adjustments.append(f"Fundamentals: +1 ({_fund_class})")
            elif _fund_score <= 3:
                total_score -= 1
                adjustments.append(f"Fundamentals: -1 ({_fund_class})")
    except Exception as _e:
        failed_layers.append('FUND')
        log_error(f"Fundamentals adjustment failed for {name}: {_e}")

    # Sentiment adjustment
    try:
        with open(f'{_LOGS}/apex-sentiment.json') as _f:
            _sent = json.load(_f)

        # Staleness gate — skip sentiment if data older than 24h
        _sent_ts = _sent.get('timestamp', '')
        try:
            from datetime import datetime, timezone, timedelta as _td
            _sent_dt  = datetime.strptime(_sent_ts, '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
            _sent_age = (datetime.now(timezone.utc) - _sent_dt).total_seconds() / 3600
            if _sent_age > 24:
                adjustments.append(f"Sentiment: skipped — data {_sent_age:.0f}h old (max 24h)")
                raise Exception("stale")
        except ValueError:
            pass

        inst_scores = _sent.get('instrument_scores', {})
        _sent_data  = inst_scores.get(name, {})
        _sentiment  = _sent_data.get('sentiment', 0)
        _label      = _sent_data.get('label', 'NEUTRAL')

        # Geo-reversal override — energy gets positive treatment during conflict
        _geo_favoured = False
        try:
            with open(f'{_SCRIPTS}/apex-quality-universe.json') as _qf:
                _qdb = json.load(_qf)
            _energy_favs = _qdb.get('geo_event_map',{}).get('iran_war',{}).get('favour',[])
            _geo_favoured = name in _energy_favs
        except Exception as _e:
            if str(_e) != 'stale':
                log_error(f"Silent failure in apex_scoring.py: {_e}")

        _sent_w = _lw.get('SENTIMENT', 1.0)

        if _geo_favoured and _sent.get('geo_status') == 'ALERT':
            adjustments.append("Sentiment: geo-override (news negative but fundamentals bullish)")
        else:
            _raw_sent_adj = (
                2 if _sentiment >= 0.3 else
                1 if _sentiment >= 0.1 else
               -2 if _sentiment <= -0.3 else
               -1 if _sentiment <= -0.1 else 0
            )
            if _raw_sent_adj != 0:
                _sent_adj = round(_raw_sent_adj * _sent_w, 2)
                total_score += _sent_adj
                _w_tag = f" [w={_sent_w}]" if _sent_w != 1.0 else ""
                _lbl   = ("VERY POSITIVE" if _sentiment >= 0.3 else
                          "POSITIVE"      if _sentiment >= 0.1 else
                          "VERY NEGATIVE" if _sentiment <= -0.3 else "NEGATIVE")
                adjustments.append(f"Sentiment: {_sent_adj:+.2g}{_w_tag} ({_lbl} {_sentiment:+.2f})")

        if _sent.get('crisis_detected', False) and signal_type == 'TREND':
            _crisis_adj = round(-2 * _sent_w, 2)
            total_score += _crisis_adj
            adjustments.append(f"Sentiment: {_crisis_adj:+.2g} (market crisis language detected)")

    except Exception as _e:
        if str(_e) != 'stale':
            failed_layers.append('SENTIMENT')
            log_error(f"Sentiment adjustment failed for {name}: {_e}")

    # Drawdown note — actual sizing handled in position sizer
    if intel['drawdown_status'] != 'NORMAL':
        adjustments.append(f"Drawdown {intel['drawdown_pct']}% — sizing at {int(intel['size_multiplier']*100)}%")

    # Layer 18: Learned score adjustment from trade outcomes history
    try:
        import importlib.util as _ilu_sa
        _spec_sa = _ilu_sa.spec_from_file_location(
            "score_adapter", f"{_SCRIPTS}/apex-score-adapter.py")
        _sa = _ilu_sa.module_from_spec(_spec_sa)
        _spec_sa.loader.exec_module(_sa)
        _learned_adj, _learned_reasons = _sa.get_learned_adjustment(signal)
        if _learned_adj != 0:
            total_score += _learned_adj
            for _lr in _learned_reasons:
                adjustments.append(_lr)
    except Exception as _e:
        failed_layers.append('ADAPTER')
        log_error(f"Score adapter failed (non-fatal): {_e}")

    # Layer 19: Adversarial exploitation boost
    # Reads apex-adversarial-results.json exploitation_opportunities; applies
    # +1 score when the current signal matches a validated positive pattern
    # (>= 15 trades, lower CI > 0.60). Capped at +1 total from this layer.
    try:
        _adv = safe_read(f'{_LOGS}/apex-adversarial-results.json', {})
        _exploit_ops = _adv.get('exploitation_opportunities', [])
        _adv_boost = 0.0
        _adv_reasons = []
        for _op in _exploit_ops:
            if _op.get('n_trades', 0) < 15:
                continue
            if _op.get('win_rate_ci', [0, 0])[0] < 0.60:
                continue
            _dims = _op.get('dimensions', {})
            _match = True
            for _dim_key, _dim_val in _dims.items():
                if _dim_key == 'signal_type' and signal_type != _dim_val:
                    _match = False; break
                elif _dim_key == 'sector':
                    _sig_sector = signal.get('sector', intel.get('sector', ''))
                    if _sig_sector != _dim_val:
                        _match = False; break
                elif _dim_key == 'rsi_bucket':
                    _rsi = signal.get('rsi', 50)
                    _rsi_b = ('<30' if _rsi < 30 else '30-45' if _rsi < 45 else
                              '45-60' if _rsi < 60 else '>60')
                    if _rsi_b != _dim_val:
                        _match = False; break
            if _match:
                _adv_boost = min(1.0, _adv_boost + 1.0)
                _adv_reasons.append(
                    f"Adversarial: +1 (validated edge: {_op.get('condition','?')[:60]} "
                    f"WR={_op.get('win_rate',0):.0%} n={_op.get('n_trades',0)})"
                )
                break  # cap at one boost
        if _adv_boost > 0:
            total_score += _adv_boost
            adjustments.extend(_adv_reasons)
    except Exception as _adv_e:
        pass  # Layer 19 is fully optional — silent failure

    # Layer confidence — how many of the 14 tracked layers ran without error
    _TOTAL_TRACKED_LAYERS = 15  # 14 original + VOL_ACCUMULATION
    layer_confidence = round(1.0 - (len(failed_layers) / _TOTAL_TRACKED_LAYERS), 2)
    if failed_layers:
        adjustments.append(
            f"Layer confidence: {layer_confidence:.0%} "
            f"({len(failed_layers)} failed: {', '.join(failed_layers)})"
        )

    # Redundancy discount — remove double-counted contributions from correlated layers.
    # Uses empirical pairwise correlations from apex-layer-audit.json.
    # Operates on the already-logged adjustments strings (non-invasive — no changes to
    # individual layer blocks above). Falls back silently when audit file absent.
    _redund_delta, _redund_notes = _apply_redundancy_discount(adjustments)
    if _redund_delta != 0:
        total_score += _redund_delta
        adjustments.extend(_redund_notes)

    # Cap total adjustment to prevent correlated alpha inflation
    total_adjustment  = total_score - base_score
    capped_adjustment = max(-5, min(5, total_adjustment))
    if total_adjustment != capped_adjustment:
        adjustments.append(f"Adjustment cap: {total_adjustment:+.1f} capped to {capped_adjustment:+.1f}")
    total_score = base_score + capped_adjustment

    raw_score    = round(total_score, 1)
    capped_score = round(min(10.0, max(0.0, total_score)), 1)

    max_expected   = 15.0
    confidence_pct = round(min(100, max(0, (raw_score / max_expected) * 100)), 1)

    signal['adjusted_score']   = capped_score
    signal['raw_score']        = raw_score
    signal['confidence_pct']   = confidence_pct
    signal['adjustments']      = adjustments
    signal['layer_confidence'] = layer_confidence
    signal['failed_layers']    = failed_layers

    return signal
