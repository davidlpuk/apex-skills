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


# ── LAYER 3: Full 18-layer signal scoring ────────────────────────────────────

def score_signal_with_intelligence(signal, intel):
    name        = signal.get('name', '')
    base_score  = float(signal.get('total_score', signal.get('contrarian_score', signal.get('score', 0))))
    signal_type = signal.get('signal_type', 'TREND')

    adjustments = []
    total_score = base_score

    # Layer: Relative strength adjustment
    try:
        _rs_mod = _MODULE_CACHE.get('rs2') or load_module('rs2', f'{_SCRIPTS}/apex-relative-strength.py')
        if _rs_mod:
            _rs_adj, _rs_reason = _rs_mod.get_rs_adjustment(name, signal_type)
            if _rs_adj != 0:
                total_score += _rs_adj
                adjustments.append(f"RS: {'+' if _rs_adj > 0 else ''}{_rs_adj} ({_rs_reason})")
    except Exception as _e:
        log_error(f"RS adjustment failed for {name}: {_e}")

    # Layer: Multi-timeframe analysis adjustment
    try:
        _mtf_mod = _MODULE_CACHE.get('mtf') or load_module('mtf', f'{_SCRIPTS}/apex-multiframe.py')
        if _mtf_mod:
            _mtf_adj, _mtf_reason = _mtf_mod.get_adjustment_for_signal(name, signal_type)
            if _mtf_adj != 0:
                total_score += _mtf_adj
                adjustments.append(f"MTF: {'+' if _mtf_adj > 0 else ''}{_mtf_adj} ({_mtf_reason[:60]})")
    except Exception as _e:
        log_error(f"MTF adjustment failed for {name}: {_e}")

    # Layer 14: Cross-asset macro confirmation
    try:
        import importlib.util as _ilu_mac
        _spec_mac = _ilu_mac.spec_from_file_location(
            "macro", f"{_SCRIPTS}/apex-macro-signals.py")
        _mac = _ilu_mac.module_from_spec(_spec_mac)
        _spec_mac.loader.exec_module(_mac)
        _mdata = safe_read(f'{_LOGS}/apex-macro-signals.json', {})
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
        log_error(f"Macro adjustment failed for {name}: {_e}")

    # Layer 14.5: Cross-asset divergence signal
    try:
        import importlib.util as _ilu_div
        _spec_div = _ilu_div.spec_from_file_location(
            "div", f"{_SCRIPTS}/apex-divergence-detector.py")
        _div = _ilu_div.module_from_spec(_spec_div)
        _spec_div.loader.exec_module(_div)
        _raw_ticker_div = signal.get('t212_ticker', signal.get('ticker', name))
        _div_adj, _div_reasons = _div.get_divergence_adjustment(name, _raw_ticker_div, signal_type)
        if _div_adj != 0:
            total_score += _div_adj
            adjustments.append(f"DIVERGE: {_div_adj:+.1f} ({_div_reasons[0][:55] if _div_reasons else ''})")
    except Exception as _e:
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
        log_error(f"Insider adjustment failed for {name}: {_e}")

    # Layer 15.5: Earnings Revision Momentum
    try:
        import importlib.util as _ilu_rev
        _spec_rev = _ilu_rev.spec_from_file_location(
            "rev", f"{_SCRIPTS}/apex-earnings-revision.py")
        _rev = _ilu_rev.module_from_spec(_spec_rev)
        _spec_rev.loader.exec_module(_rev)
        _raw_ticker_rev = signal.get('t212_ticker', signal.get('ticker', name))
        _rev_adj, _rev_reasons = _rev.get_revision_momentum(name, _raw_ticker_rev, signal_type)
        if _rev_adj != 0:
            total_score += _rev_adj
            adjustments.append(f"REVISION: {_rev_adj:+.1f} ({_rev_reasons[0][:55] if _rev_reasons else ''})")
    except Exception as _e:
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
            total_score += _fred_adj
            adjustments.append(f"FRED: {_fred_adj:+d} ({_fred_reasons[0][:55] if _fred_reasons else ''})")
    except Exception as _e:
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
        _raw_ticker  = signal.get('t212_ticker', signal.get('ticker', name))
        _opts_ticker = _T212_TO_YAHOO_OPTS.get(
            _raw_ticker,
            _raw_ticker.replace('_US_EQ','').replace('_EQ','')
        )
        if _opts_ticker in _OPTS_UNIVERSE:
            _opts_adj, _opts_reasons = _opts.get_options_adjustment(_opts_ticker, signal_type)
            if _opts_adj != 0:
                total_score += _opts_adj
                adjustments.append(f"OPTIONS: {_opts_adj:+d} ({_opts_reasons[0][:55] if _opts_reasons else ''})")
    except Exception as _e:
        log_error(f"Options flow adjustment failed for {name}: {_e}")

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
        log_error(f"Silent failure in apex_scoring.py: {_e}")

    # Backtest instrument boost
    try:
        with open(f'{_LOGS}/apex-backtest-insights.json') as _f:
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
        log_error(f"Silent failure in apex_scoring.py: {_e}")

    # Sector rotation boost
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
        log_error(f"Silent failure in apex_scoring.py: {_e}")

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
        log_error(f"Silent failure in apex_scoring.py: {_e}")

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
            log_error(f"Silent failure in apex_scoring.py: {_e}")

        if _geo_favoured and _sent.get('geo_status') == 'ALERT':
            adjustments.append("Sentiment: geo-override (news negative but fundamentals bullish)")
        elif _sentiment >= 0.3:
            total_score += 2
            adjustments.append(f"Sentiment: +2 (VERY POSITIVE {_sentiment:+.2f})")
        elif _sentiment >= 0.1:
            total_score += 1
            adjustments.append(f"Sentiment: +1 (POSITIVE {_sentiment:+.2f})")
        elif _sentiment <= -0.3:
            total_score -= 2
            adjustments.append(f"Sentiment: -2 (VERY NEGATIVE {_sentiment:+.2f})")
        elif _sentiment <= -0.1:
            total_score -= 1
            adjustments.append(f"Sentiment: -1 (NEGATIVE {_sentiment:+.2f})")

        if _sent.get('crisis_detected', False) and signal_type == 'TREND':
            total_score -= 2
            adjustments.append("Sentiment: -2 (market crisis language detected)")

    except Exception as _e:
        log_error(f"Silent failure in apex_scoring.py: {_e}")

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
        log_error(f"Score adapter failed (non-fatal): {_e}")

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

    signal['adjusted_score'] = capped_score
    signal['raw_score']      = raw_score
    signal['confidence_pct'] = confidence_pct
    signal['adjustments']    = adjustments

    return signal
