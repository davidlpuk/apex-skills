#!/usr/bin/env python3
"""
Apex Unified Decision Engine
Orchestrates all intelligence layers into a single decision pass.
Runs at 08:30 daily, replacing apex-morning-scan.sh
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
import sys as _sys
_sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, log_info, send_telegram, get_portfolio_value, get_free_cash
    from apex_intelligence import gather_intelligence
    from apex_scoring import score_signal_with_intelligence, get_instrument_sector, load_module as _load_module, _MODULE_CACHE
    from apex_filters import is_blocked
    from apex_sizer import calculate_final_position
except ImportError as _ie:
    print(f"WARN: module import partial — {_ie}")
    def atomic_write(p, d):
        import json
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')


# File paths
REGIME_FILE      = '/home/ubuntu/.picoclaw/logs/apex-regime.json'
GEO_FILE         = '/home/ubuntu/.picoclaw/logs/apex-geo-news.json'
DIRECTION_FILE   = '/home/ubuntu/.picoclaw/logs/apex-market-direction.json'
SECTOR_ROT_FILE  = '/home/ubuntu/.picoclaw/logs/apex-sector-rotation.json'
BREADTH_FILE     = '/home/ubuntu/.picoclaw/logs/apex-breadth-drilldown.json'
VIX_CORR_FILE    = '/home/ubuntu/.picoclaw/logs/apex-vix-correlation.json'
DRAWDOWN_FILE    = '/home/ubuntu/.picoclaw/logs/apex-drawdown.json'
EARNINGS_FILE    = '/home/ubuntu/.picoclaw/logs/apex-earnings-flags.json'
NEWS_FILE        = '/home/ubuntu/.picoclaw/logs/apex-news-flags.json'
DRIFT_FILE       = '/home/ubuntu/.picoclaw/logs/apex-earnings-drift.json'
DIVIDEND_FILE    = '/home/ubuntu/.picoclaw/logs/apex-dividend-capture.json'
POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
SIGNAL_FILE      = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
DECISION_LOG     = '/home/ubuntu/.picoclaw/logs/apex-decision-log.json'
TICKER_MAP       = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'
QUALITY_FILE     = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'


# ── Module cache — module-level so score_signal_with_intelligence() can access it ──
# Previously this was a local inside run(), causing NameError for RS and MTF layers
# (silently caught by except blocks, so RS and MTF scored nothing on every signal).
_MODULE_CACHE = {}
_rs_mod  = None
_mtf_mod = None


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default or {}

# gather_intelligence() → apex_intelligence.py
# score_signal_with_intelligence() → apex_scoring.py
# is_blocked() → apex_filters.py
# calculate_final_position() → apex_sizer.py

# ============================================================
# (REMOVED) LAYER 1 — now in apex_intelligence.py
# ============================================================

def _gather_intelligence_stub():
    intel = {}

    # Regime
    regime = load_json(REGIME_FILE)
    intel['vix']            = float(regime.get('vix', 20))
    intel['breadth']        = float(regime.get('breadth_pct', 50))
    intel['regime_status']  = regime.get('overall', 'CLEAR')
    intel['regime_reasons'] = regime.get('block_reason', [])

    # Geo
    geo = load_json(GEO_FILE)
    intel['geo_status']       = geo.get('overall', 'CLEAR')
    intel['geo_energy_flags'] = geo.get('energy_flags', [])
    intel['geo_flags']        = geo.get('geo_flags', [])

    # Market direction
    direction = load_json(DIRECTION_FILE)
    intel['direction_status'] = direction.get('overall', 'CLEAR')
    intel['direction_blocks'] = direction.get('blocks', [])

    # Sector rotation
    sector_rot = load_json(SECTOR_ROT_FILE)
    sectors    = sector_rot.get('sectors', [])
    intel['leading_sectors'] = sector_rot.get('leaders', [])
    intel['lagging_sectors'] = sector_rot.get('laggards', [])
    intel['sector_scores']   = {s['name']: s['score'] for s in sectors}

    # Sector breadth
    breadth_data = load_json(BREADTH_FILE)
    intel['sector_breadth'] = breadth_data.get('sectors', {})
    intel['strongest_sector'] = breadth_data.get('strongest')
    intel['weakest_sector']   = breadth_data.get('weakest')

    # VIX correlation of current positions
    vix_corr = load_json(VIX_CORR_FILE)
    intel['position_vix_sensitivity'] = {
        p['ticker']: p['vix_corr']
        for p in vix_corr.get('positions', [])
    }

    # Drawdown
    drawdown = load_json(DRAWDOWN_FILE)
    intel['drawdown_pct']    = drawdown.get('drawdown_pct', 0)
    intel['drawdown_status'] = drawdown.get('status', 'NORMAL')
    intel['size_multiplier'] = drawdown.get('multiplier', 1.0)

    # Earnings and news flags
    try:
        with open(EARNINGS_FILE) as f:
            earnings_flags = json.load(f)
        intel['earnings_blocked'] = [d['name'] if isinstance(d, dict) else d for d in earnings_flags]
    except Exception:
        intel['earnings_blocked'] = []

    try:
        with open(NEWS_FILE) as f:
            intel['news_blocked'] = json.load(f)
    except Exception:
        intel['news_blocked'] = []

    # Drift signals
    drift = load_json(DRIFT_FILE)
    intel['drift_signals'] = drift.get('signals', [])

    # Dividend signals
    dividend = load_json(DIVIDEND_FILE)
    intel['dividend_signals'] = dividend.get('signals', [])

    # Open positions
    intel['open_positions'] = load_json(POSITIONS_FILE, [])

    return intel

# ============================================================
# LAYER 2 — SECTOR BOOST/PENALTY
# ============================================================

SECTOR_MAP = {
    "Energy":     ["XOM","CVX","SHEL","BP","TTE","IUES","NG_EQ","SSE_EQ","INRG","NFE"],
    "Technology": ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","AMD","CRM","ORCL","QCOM","IITU","CRDO","IUIT"],
    "Financials": ["JPM","GS_EQ","MS_EQ","BAC","BLK","V_US","AXP","HSBA","BARC","NWG","IUFS","PGY"],
    "Healthcare": ["JNJ","PFE","MRK","UNH","ABBV","AZN","GSK","TMO","DHR","IUHC","NOVO"],
    "Consumer":   ["KO","PEP","MCD","WMT","PG","DGE","ULVR","CPG","IMB","BATS","IUCD"],
}

def get_instrument_sector(name):
    name_upper = name.upper().strip()
    for sector, instruments in SECTOR_MAP.items():
        for inst in instruments:
            inst_upper = inst.upper().strip()
            # Exact match or ticker match
            if name_upper == inst_upper or name_upper == inst_upper.replace("_EQ","").replace("_US",""):
                return sector
            # Full name contains ticker
            if len(inst_upper) <= 5 and inst_upper in name_upper.split():
                return sector
    return None

def get_sector_boost(name, intel):
    sector = get_instrument_sector(name)
    if not sector:
        return 0, "Unknown sector"

    sector_score    = intel['sector_scores'].get(sector, 5)
    sector_breadth  = intel['sector_breadth'].get(sector, {})
    breadth_200     = sector_breadth.get('breadth_200', 50)
    health          = sector_breadth.get('health', 'NEUTRAL')

    boost = 0
    reasons = []

    # Sector rotation boost
    if sector in intel['leading_sectors']:
        boost += 2
        reasons.append(f"{sector} is leading sector ({sector_score}/10 rotation score)")
    elif sector in intel['lagging_sectors']:
        boost -= 1
        reasons.append(f"{sector} is lagging sector ({sector_score}/10 rotation score)")

    # Sector breadth adjustment
    if breadth_200 >= 60:
        boost += 1
        reasons.append(f"{sector} breadth strong ({breadth_200}% above 200 EMA)")
    elif breadth_200 <= 25:
        boost -= 1
        reasons.append(f"{sector} breadth weak ({breadth_200}% above 200 EMA)")

    # Health gate
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

    # Exact ticker match only for geo boost
    name_upper = name.upper().strip()
    is_favoured = any(
        name_upper == f.upper() or
        name_upper in f.upper().split('_') or
        f.upper() in name_upper.split('_')
        for f in favoured
    )
    is_avoided = any(
        name_upper == a.upper()
        for a in avoided
    )

    if is_favoured:
        return 2, [f"Geo-reversal: {name} benefits from current conflict"]
    elif is_avoided:
        return -2, [f"Geo risk: {name} hurt by current conflict"]

    return 0, []

# ============================================================
# LAYER 3 — SIGNAL SCORING WITH FULL INTELLIGENCE
# ============================================================

def score_signal_with_intelligence(signal, intel):
    name        = signal.get('name', '')
    base_score  = float(signal.get('total_score', signal.get('contrarian_score', signal.get('score', 0))))
    signal_type = signal.get('signal_type', 'TREND')

    adjustments = []
    total_score = base_score

    # Relative strength adjustment
    # Relative strength adjustment
    try:
        _rs2_cached = _MODULE_CACHE.get('rs2', _rs_mod)
        if _rs2_cached:
            _rs_adj, _rs_reason = _rs2_cached.get_rs_adjustment(name, signal_type)
            if _rs_adj != 0:
                total_score += _rs_adj
                adjustments.append(f"RS: {'+' if _rs_adj > 0 else ''}{_rs_adj} ({_rs_reason})")
    except Exception as _e:
        log_error(f"RS adjustment failed for {name}: {_e}")

    # Multi-timeframe analysis adjustment
    # Multi-timeframe analysis adjustment
    try:
        _mtf_cached = _MODULE_CACHE.get('mtf', _mtf_mod)
        if _mtf_cached:
            _mtf_adj, _mtf_reason = _mtf_cached.get_adjustment_for_signal(name, signal_type)
            if _mtf_adj != 0:
                total_score += _mtf_adj
                adjustments.append(f"MTF: {'+' if _mtf_adj > 0 else ''}{_mtf_adj} ({_mtf_reason[:60]})")
    except Exception as _e:
        log_error(f"MTF adjustment failed for {name}: {_e}")

    # Layer 14: Cross-asset macro confirmation
    try:
        import importlib.util as _ilu_mac
        _spec_mac = _ilu_mac.spec_from_file_location(
            "macro", "/home/ubuntu/.picoclaw/scripts/apex-macro-signals.py")
        _mac = _ilu_mac.module_from_spec(_spec_mac)
        _spec_mac.loader.exec_module(_mac)
        _mdata = safe_read('/home/ubuntu/.picoclaw/logs/apex-macro-signals.json', {})
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

    # Layer 15: EDGAR Insider Data
    try:
        import importlib.util as _ilu_ins
        _spec_ins = _ilu_ins.spec_from_file_location(
            "ins", "/home/ubuntu/.picoclaw/scripts/apex-insider-data.py")
        _ins = _ilu_ins.module_from_spec(_spec_ins)
        _spec_ins.loader.exec_module(_ins)
        _ins_adj, _ins_reasons = _ins.get_insider_adjustment(name, signal_type)
        if _ins_adj != 0:
            total_score += _ins_adj
            adjustments.append(f"INSIDER: +{_ins_adj} ({_ins_reasons[0][:55] if _ins_reasons else ''})")
    except Exception as _e:
        log_error(f"Insider adjustment failed for {name}: {_e}")

    # Layer 16: FRED Macro Economic Signal
    try:
        import importlib.util as _ilu_fred
        _spec_fred = _ilu_fred.spec_from_file_location(
            "fred", "/home/ubuntu/.picoclaw/scripts/apex-fred-macro.py")
        _fred = _ilu_fred.module_from_spec(_spec_fred)
        _spec_fred.loader.exec_module(_fred)
        _fred_adj, _fred_reasons = _fred.get_fred_adjustment(signal_type)
        if _fred_adj != 0:
            total_score += _fred_adj
            adjustments.append(f"FRED: {_fred_adj:+d} ({_fred_reasons[0][:55] if _fred_reasons else ''})")
    except Exception as _e:
        log_error(f"FRED adjustment failed for {name}: {_e}")

    # Layer 17: Options Flow Signal
    # Normalise ticker to Yahoo format (options data is keyed by "V", "XOM", "AAPL" etc.)
    try:
        import importlib.util as _ilu_opts
        _spec_opts = _ilu_opts.spec_from_file_location(
            "opts", "/home/ubuntu/.picoclaw/scripts/apex-options-flow.py")
        _opts = _ilu_opts.module_from_spec(_spec_opts)
        _spec_opts.loader.exec_module(_opts)

        # Build Yahoo ticker from T212 ticker or name — strips suffixes then checks options universe
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
        # Only query if ticker is in the options universe
        if _opts_ticker in _OPTS_UNIVERSE:
            _opts_adj, _opts_reasons = _opts.get_options_adjustment(_opts_ticker, signal_type)
            if _opts_adj != 0:
                total_score += _opts_adj
                adjustments.append(f"OPTIONS: {_opts_adj:+d} ({_opts_reasons[0][:55] if _opts_reasons else ''})")
    except Exception as _e:
        log_error(f"Options flow adjustment failed for {name}: {_e}")

    # Breadth thrust regime adjustment to signal
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-breadth-thrust.json') as _bt_f:
            _bt_data = json.load(_bt_f)
        _bt_signal = _bt_data.get('composite_signal', 0)
        if _bt_signal >= 3 and signal_type == 'TREND':
            total_score += 1
            adjustments.append("Breadth thrust: +1 (rare bull signal active)")
        elif _bt_signal <= -2 and signal_type == 'CONTRARIAN':
            total_score -= 1
            adjustments.append(f"Breadth: -1 (deterioration {_bt_data.get('divergence',{}).get('breadth_trend',0):+.1f}%)")
    except Exception as _e:
        log_error(f"Silent failure in apex-decision-engine.py: {_e}")

    # Backtest instrument boost
    backtest_boost = 0
    backtest_reason = ""
    try:
        import json as _j
        with open('/home/ubuntu/.picoclaw/logs/apex-backtest-insights.json') as _f:
            bt = _j.load(_f)
        signal_type_key = 'trend_strategy' if signal_type == 'TREND' else 'contrarian_strategy'
        best  = bt.get(signal_type_key, {}).get('best_instruments', [])
        worst = bt.get(signal_type_key, {}).get('worst_instruments', [])
        if name in best:
            backtest_boost  = 1
            backtest_reason = f"Backtest validated: top performer for {signal_type}"
        elif name in worst:
            backtest_boost  = -1
            backtest_reason = f"Backtest warning: poor performer for {signal_type}"
        if backtest_boost != 0:
            total_score += backtest_boost
            adjustments.append(f"Backtest: {'+' if backtest_boost > 0 else ''}{backtest_boost} ({backtest_reason})")
    except Exception as _e:
        log_error(f"Silent failure in apex-decision-engine.py: {_e}")

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

    # Fix 7: geo-correlation cap — geo and sector boosts driven by same event must not both count fully
    try:
        _geo_active = (intel.get('geo', {}).get('overall', 'CLEAR') == 'ALERT')
        if _geo_active and geo_boost > 0 and sector_boost > 0:
            _combined_geo_stack = geo_boost + sector_boost
            _geo_cap = 3  # Max combined geo+sector boost during any single geo event
            if _combined_geo_stack > _geo_cap:
                _excess = _combined_geo_stack - _geo_cap
                total_score -= _excess
                adjustments.append(
                    f"Geo-correlation cap: -{_excess} "
                    f"(geo +{geo_boost} + sector +{sector_boost} capped at +{_geo_cap} — same event)"
                )
    except Exception:
        pass

    # VIX sensitivity penalty — reduce size on high sensitivity instruments
    vix_corr = intel['position_vix_sensitivity'].get(
        next((p.get('t212_ticker','') for p in intel['open_positions'] if name in p.get('name','')), ''),
        -0.2
    )
    if vix_corr <= -0.6 and intel['vix'] >= 25:
        total_score -= 1
        adjustments.append(f"VIX sensitivity penalty: -1 (corr {vix_corr}, VIX {intel['vix']})")

    # Advanced fundamental signals (5-factor composite)
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-fundamental-signals.json') as _ff:
            _fsig = json.load(_ff)
        _fdata = _fsig.get('data', {}).get(name, {})
        _comp_score = _fdata.get('composite_score', 0)
        _composite  = _fdata.get('composite', 'NEUTRAL')
        _freasons   = _fdata.get('reasons', [])

        # Short interest context — boost contrarian on high short, penalise trend
        _short = _fdata.get('short_interest', {})
        _short_pct = _short.get('short_pct_float', 0) if _short else 0

        if signal_type == 'CONTRARIAN':
            # Apply full composite score for contrarian
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

            # Short squeeze boost for contrarian
            if _short_pct > 10:
                total_score += 1
                adjustments.append(f"Short squeeze: +1 ({round(_short_pct,1)}% short float)")

        elif signal_type == 'TREND':
            # Lighter touch for trend
            if _comp_score >= 3:
                total_score += 1
                adjustments.append(f"Fundamentals5: +1 ({_composite})")
            elif _comp_score <= -2:
                total_score -= 1
                adjustments.append(f"Fundamentals5: -1 ({_composite})")

            # High short interest penalty for trend
            if _short_pct > 15:
                total_score -= 1
                adjustments.append(f"Short interest: -1 ({round(_short_pct,1)}% float short — smart money bearish)")

    except Exception as _e:
        log_error(f"Silent failure in apex-decision-engine.py: {_e}")

    # Fundamental data boost (legacy EV/EBITDA score)
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-fundamentals.json') as _ff:
            _fund_db = json.load(_ff)
        _fund_data = _fund_db.get('data', {}).get(name, {})
        _fund_score = _fund_data.get('fund_score', 5)
        _fund_class = _fund_data.get('fund_class', 'NEUTRAL')
        _fund_reasons = _fund_data.get('reasons', [])

        if signal_type in ['CONTRARIAN', 'EARNINGS_DRIFT']:
            # Fundamentals matter more for contrarian — buying a falling stock
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
            # Fundamentals less critical for trend but still relevant
            if _fund_score >= 9:
                total_score += 1
                adjustments.append(f"Fundamentals: +1 ({_fund_class})")
            elif _fund_score <= 3:
                total_score -= 1
                adjustments.append(f"Fundamentals: -1 ({_fund_class})")
    except Exception as _e:
        log_error(f"Silent failure in apex-decision-engine.py: {_e}")

    # Sentiment adjustment
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-sentiment.json') as _f:
            _sent = json.load(_f)

        # Fix 10: staleness gate — skip sentiment if data is older than 24h
        _sent_ts  = _sent.get('timestamp', '')
        _sent_age = 999  # default: treat as stale if unparseable
        try:
            from datetime import timedelta as _td
            _sent_dt  = datetime.strptime(_sent_ts, '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
            _sent_age = (datetime.now(timezone.utc) - _sent_dt).total_seconds() / 3600
            if _sent_age > 24:
                adjustments.append(f"Sentiment: skipped — data {_sent_age:.0f}h old (max 24h)")
                raise Exception("stale")
        except ValueError:
            pass  # Can't parse timestamp — proceed normally

        inst_scores = _sent.get('instrument_scores', {})

        # Find sentiment for this instrument
        _sent_data = inst_scores.get(name, {})
        _sentiment = _sent_data.get('sentiment', 0)
        _label     = _sent_data.get('label', 'NEUTRAL')

        # Geo-reversal override — energy gets positive treatment during conflict
        # even if news sentiment is negative (war = negative headlines but bullish fundamentals)
        _geo_favoured = False
        try:
            with open('/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json') as _qf:
                _qdb = json.load(_qf)
            _energy_favs = _qdb.get('geo_event_map',{}).get('iran_war',{}).get('favour',[])
            _geo_favoured = name in _energy_favs
        except Exception as _e:
            log_error(f"Silent failure in apex-decision-engine.py: {_e}")

        if _geo_favoured and _sent.get('geo_status') == 'ALERT':
            # Don't penalise energy during geo conflict
            adjustments.append(f"Sentiment: geo-override (news negative but fundamentals bullish)")
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

        # Crisis detection — reduce all scores (only if data is fresh, <12h)
        if _sent.get('crisis_detected', False) and signal_type == 'TREND' and _sent_age < 12:
            total_score -= 2
            adjustments.append("Sentiment: -2 (market crisis language detected)")

    except Exception as _e:
        log_error(f"Silent failure in apex-decision-engine.py: {_e}")

    # Drawdown adjustment — note only, actual sizing handled in position sizer
    if intel['drawdown_status'] != 'NORMAL':
        adjustments.append(f"Drawdown {intel['drawdown_pct']}% — sizing at {int(intel['size_multiplier']*100)}%")

    # ── Layer 18: Learned score adjustment from trade outcomes history ──
    # Reads apex-scoring-weights.json built by apex-score-adapter.py.
    # Silent pass-through until MIN_TRADES_PER_CATEGORY reached per bucket.
    try:
        import importlib.util as _ilu_sa
        _spec_sa = _ilu_sa.spec_from_file_location(
            "score_adapter", "/home/ubuntu/.picoclaw/scripts/apex-score-adapter.py")
        _sa = _ilu_sa.module_from_spec(_spec_sa)
        _spec_sa.loader.exec_module(_sa)
        _learned_adj, _learned_reasons = _sa.get_learned_adjustment(signal)
        if _learned_adj != 0:
            total_score += _learned_adj
            for _lr in _learned_reasons:
                adjustments.append(_lr)
    except Exception as _e:
        log_error(f"Score adapter failed (non-fatal): {_e}")
    # ── end Layer 18 ───────────────────────────────────────────────────

    # Cap total adjustment to prevent correlated alpha inflation
    total_adjustment = total_score - base_score
    capped_adjustment = max(-5, min(5, total_adjustment))
    if total_adjustment != capped_adjustment:
        adjustments.append(f"Adjustment cap: {total_adjustment:+.1f} capped to {capped_adjustment:+.1f}")
    total_score = base_score + capped_adjustment

    # Raw score — shows full intelligence picture
    raw_score = round(total_score, 1)

    # Capped score — used for gating and comparison (0-10 scale)
    capped_score = round(min(10.0, max(0.0, total_score)), 1)

    # Confidence percentage — how strongly intelligence supports this signal
    # Raw score of 7 = 100% (threshold), higher = more confidence
    # Scale: threshold (7) = 70%, max reasonable (15) = 100%
    base_threshold = 7.0
    max_expected   = 15.0
    confidence_pct = round(min(100, max(0,
        (raw_score / max_expected) * 100
    )), 1)

    signal['adjusted_score']  = capped_score      # Used for filtering/selection
    signal['raw_score']       = raw_score          # Full picture including all boosts
    signal['confidence_pct']  = confidence_pct     # 0-100% confidence
    signal['adjustments']     = adjustments

    return signal

# ============================================================
# LAYER 4 — SIGNAL GENERATION
# ============================================================

def run_trend_scan():
    result = subprocess.run(
        ['python3', '/home/ubuntu/.picoclaw/scripts/apex-market-data.py'],
        capture_output=True, text=True, timeout=180
    )
    output = result.stdout
    start  = output.find('=== FULL DATA ===')
    if start == -1:
        return []
    try:
        data = json.loads(output[start + len('=== FULL DATA ==='):].strip())
        return [x for x in data if x.get('total_score', 0) >= 6 and 'error' not in x]
    except Exception:
        return []

def run_contrarian_scan(intel):
    result = subprocess.run(
        ['python3', '/home/ubuntu/.picoclaw/scripts/apex-contrarian-scan.py'],
        capture_output=True, text=True, timeout=300
    )
    output = result.stdout
    start  = output.find('=== FULL DATA ===')
    if start == -1:
        return []
    try:
        data = json.loads(output[start + len('=== FULL DATA ==='):].strip())
        return [x for x in data if x.get('contrarian_score', 0) >= 5 and 'error' not in x]
    except Exception:
        return []

# ============================================================
# LAYER 5 — SIGNAL FILTERING
# ============================================================

def is_blocked(signal, intel):
    name        = signal.get('name', '')
    signal_type = signal.get('signal_type', 'TREND')
    blocks      = []

    # Earnings block
    if name in intel['earnings_blocked']:
        blocks.append(f"Earnings block: {name}")

    # News block
    if name in intel['news_blocked']:
        blocks.append(f"News block: {name}")

    # Sector breadth block — only for trend signals
    if signal_type == 'TREND':
        sector = get_instrument_sector(name)
        if sector:
            breadth = intel['sector_breadth'].get(sector, {})
            if breadth.get('breadth_200', 50) <= 20:
                blocks.append(f"Sector breadth too low: {sector} at {breadth.get('breadth_200',0)}%")

    # Regime block — trend signals only
    if signal_type == 'TREND' and intel['regime_status'] == 'BLOCKED':
        blocks.append(f"Regime blocked: VIX {intel['vix']} | Breadth {intel['breadth']}%")

    # Geo block — non-favoured instruments only
    if intel['geo_status'] == 'ALERT':
        geo_boost, _ = get_geo_adjustment(name, intel)
        if geo_boost < 0:
            blocks.append(f"Geo risk: {name} hurt by current conflict")

    # Market direction block — trend signals only
    if signal_type == 'TREND' and intel['direction_status'] == 'BLOCKED':
        blocks.append(f"Market direction: {' | '.join(intel['direction_blocks'])}")

    return blocks

# ============================================================
# LAYER 6 — POSITION SIZING WITH FULL CONTEXT
# ============================================================

def calculate_final_position(signal, intel):
    entry = float(signal.get('entry', signal.get('price', 0)))
    stop  = float(signal.get('stop', entry * 0.94))

    if entry <= 0 or stop <= 0:
        return 1, 50

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 1, entry

    # Continuous regime scaling — replaces binary VIX thresholds
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("rs", "/home/ubuntu/.picoclaw/scripts/apex-regime-scaling.py")
        _rs   = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_rs)
        regime_scale = _rs.get_scale_for_signal(signal.get('signal_type','TREND'))
    except Exception:
        regime_scale = 0.5

    # Risk budget: 1% of live portfolio value, scaled by regime
    # Falls back to £50 if portfolio value unavailable
    portfolio_value = get_portfolio_value() or 5000
    risk_pct        = 0.01   # 1% of portfolio per trade
    base_risk       = round(portfolio_value * risk_pct * regime_scale, 2)
    # Floor: £5 (avoid sub-penny qty), ceiling: 1.5% of portfolio
    base_risk       = max(5.0, min(portfolio_value * 0.015, base_risk))

    vix = intel['vix']  # Keep for reference

    # Score conviction
    score     = signal.get('adjusted_score', signal.get('total_score', 7))
    max_score = 12  # With boosts, max possible
    conviction = score / max_score

    # Signal type
    if signal.get('signal_type') == 'CONTRARIAN':
        conviction *= 0.8
    elif signal.get('signal_type') == 'EARNINGS_DRIFT':
        conviction *= 1.1
    elif signal.get('signal_type') == 'DIVIDEND_CAPTURE':
        conviction *= 0.9

    # Performance + momentum feedback from position sizer module
    try:
        from apex_position_sizer import _performance_multiplier, _momentum_multiplier
        _perf_m = _performance_multiplier()
        _mom_m  = _momentum_multiplier()
        _combined_feedback = max(0.5, min(1.5, _perf_m * _mom_m))
    except Exception:
        _perf_m = _mom_m = _combined_feedback = 1.0

    # Drawdown adjustment (intel['size_multiplier'] is the drawdown multiplier)
    risk_amount = base_risk * conviction * _combined_feedback * intel['size_multiplier']
    # Floor £5, ceiling 1.5% of portfolio
    risk_amount = max(5.0, min(portfolio_value * 0.015, round(risk_amount, 2)))

    # ── Kelly Criterion overlay ────────────────────────────────────────
    # Load the Kelly recommendation from apex-thorp-test.py.
    # When using backtest priors: Kelly acts as a soft cap (don't exceed by >20%).
    # When using real trade data (50+ trades): Kelly recommended_risk used directly.
    # On negative Kelly (no edge): signal is still allowed but sized at minimum.
    try:
        import importlib.util as _ilu_k
        _spec_k = _ilu_k.spec_from_file_location(
            "thorp", "/home/ubuntu/.picoclaw/scripts/apex-thorp-test.py")
        _thorp = _ilu_k.module_from_spec(_spec_k)
        _spec_k.loader.exec_module(_thorp)
        _kelly = _thorp.calculate_optimal_size(signal, portfolio_value)

        if _kelly and _kelly.get('verdict') != 'ABORT':
            kelly_risk    = _kelly['recommended_risk']
            using_prior   = _kelly['using_prior']

            if not using_prior:
                # Real data — use Kelly directly, keep conviction & regime scaling
                risk_amount = round(min(risk_amount, kelly_risk), 2)
                log_info(f"Kelly (real data, {_kelly['sample_count']} trades): "
                         f"£{kelly_risk} → using £{risk_amount}")
            else:
                # Priors — use Kelly as a soft cap (allow up to 120% of Kelly prior)
                kelly_soft_cap = round(kelly_risk * 1.2, 2)
                if risk_amount > kelly_soft_cap:
                    risk_amount = kelly_soft_cap
                    log_info(f"Kelly (prior): soft-capped risk at £{risk_amount}")

        elif _kelly and _kelly.get('verdict') == 'ABORT':
            # Negative Kelly — still trade but at minimum size (no mathematical edge)
            risk_amount = max(5.0, portfolio_value * 0.002)
            log_warning(f"Kelly ABORT for {signal.get('name','?')}: "
                        f"{_kelly.get('verdict_reason','')} — sizing at minimum")

    except Exception as _ke:
        log_error(f"Kelly overlay failed (non-fatal): {_ke}")
    # ── end Kelly overlay ──────────────────────────────────────────────

    qty      = round(risk_amount / risk_per_share, 2)
    notional = round(qty * entry, 2)

    # Cap notional at 8% of portfolio
    max_notional = portfolio_value * 0.08
    if notional > max_notional:
        qty      = round(max_notional / entry, 2)
        notional = round(qty * entry, 2)

    # ── Cash reserve enforcement ───────────────────────────────────────
    # Never commit more than 90% of free cash to a single trade.
    # Protects against limit orders tying up all available capital.
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
    # ── end cash reserve ───────────────────────────────────────────────

    return qty, notional

# ============================================================
# LAYER 7 — SAVE SIGNAL AND NOTIFY
# ============================================================

def save_and_notify(signal, intel, qty, notional):
    name        = signal.get('name', '?')
    signal_type = signal.get('signal_type', 'TREND')
    entry       = float(signal.get('entry', signal.get('price', 0)))
    stop        = float(signal.get('stop', entry * 0.94))
    score       = signal.get('adjusted_score', signal.get('total_score', 0))
    adjustments = signal.get('adjustments', [])
    rsi         = signal.get('rsi', 0)

    # Get T212 ticker
    try:
        with open(TICKER_MAP) as f:
            tmap = json.load(f)
        t212 = tmap.get(name, {}).get('t212', '')
        full_name = tmap.get(name, {}).get('name', name)
    except Exception:
        t212, full_name = '', name

    target1 = signal.get('target1', round(entry + (entry - stop) * 1.5, 2))
    target2 = signal.get('target2', round(entry + (entry - stop) * 2.5, 2))

    # Fix 2: CONTRARIAN with RSI > 30 is not genuinely oversold — reclassify as
    # GEO_REVERSAL so it gets the correct stop multiplier and labelling
    reclassified = None
    if signal_type == 'CONTRARIAN' and float(rsi) > 30:
        signal_type  = 'GEO_REVERSAL'
        reclassified = f"RSI {rsi} > 30 — reclassified CONTRARIAN→GEO_REVERSAL"

    pending = {
        "name":         full_name,
        "t212_ticker":  t212,
        "quantity":     qty,
        "entry":        entry,
        "stop":         stop,
        "target1":      target1,
        "target2":      target2,
        "score":        score,
        "adjusted_score": signal.get("adjusted_score", score),
        "raw_score":    signal.get("raw_score", score),
        "confidence_pct": signal.get("confidence_pct", 0),
        "rsi":          rsi,
        "macd":         signal.get('macd_hist', 0),
        "sector":       get_instrument_sector(name) or 'UNKNOWN',
        "signal_type":  signal_type,
        "reclassified": reclassified,
        "currency":     signal.get('currency', 'USD'),
        "adjustments":  adjustments,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

    atomic_write(SIGNAL_FILE, pending)

    # Build notification
    type_icon  = "🔄" if signal_type == 'CONTRARIAN' else ("💰" if signal_type == 'DIVIDEND_CAPTURE' else ("📊" if signal_type == 'EARNINGS_DRIFT' else "📈"))
    type_label = signal_type.replace('_', ' ')
    risk       = round(qty * (entry - stop), 2)

    adj_str = ""
    if adjustments:
        adj_str = "\n🧠 Intelligence:\n" + "\n".join(f"  • {a}" for a in adjustments[:3])

    ev        = signal.get('ev', None)
    ev_verdict = signal.get('ev_verdict', '')
    r_expect  = signal.get('r_expectancy', '')
    bk_wr     = signal.get('breakeven_wr', '')
    ev_str    = ""
    if ev is not None:
        ev_icon = "✅" if ev_verdict == 'POSITIVE' else "❌"
        ev_str  = f"\n\n📐 MATHEMATICS:\n  EV: £{ev} {ev_icon}\n  R-expectancy: {r_expect}R\n  Breakeven win rate: {round(float(bk_wr)*100,1)}%"

    msg = (
        f"📊 APEX SIGNAL — {datetime.now(timezone.utc).strftime('%a %d %b %Y')}\n\n"
        f"{type_icon} {type_label}: {full_name}\n"
        f"{'='*35}\n"
        f"💰 Entry:   £{entry}\n"
        f"🛑 Stop:    £{stop}\n"
        f"🎯 T1:      £{target1}\n"
        f"🎯 T2:      £{target2}\n"
        f"📐 Qty:     {qty} shares (£{notional})\n"
        f"⚠️  Risk:    £{risk}\n"
        f"📊 Score:   {signal.get('adjusted_score', score)}/10 (raw:{signal.get('raw_score', score)} | {signal.get('confidence_pct', 0)}% conf)\n"
        f"📈 RSI:     {rsi}"
        f"{adj_str}"
        f"{ev_str}\n\n"
        f"🤖 Autopilot will execute automatically\n"
        f"Reply REJECT to skip this signal"
    )

    send_telegram(msg)
    return pending

# ============================================================
# DECISION LOGGING — Persist every run for auditability
# ============================================================

def log_decision_run(all_signals, blocked_map, qualified, best, intel):
    """
    Append a record of this decision run to apex-decision-log.json.
    blocked_map: {signal_name: [block_reason, ...]}
    """
    try:
        log = safe_read(DECISION_LOG, [])
        if not isinstance(log, list):
            log = []

        signals_log = []
        for s in all_signals:
            name = s.get('name', '?')
            entry = {
                "name":         name,
                "signal_type":  s.get('signal_type', ''),
                "raw_score":    s.get('raw_score', s.get('adjusted_score', 0)),
                "adj_score":    s.get('adjusted_score', 0),
                "rsi":          s.get('rsi', 0),
                "adjustments":  s.get('adjustments', []),
                "blocked":      bool(blocked_map.get(name)),
                "block_reasons": blocked_map.get(name, []),
            }
            signals_log.append(entry)

        _ages = intel.get('file_ages_hours', {})
        run_record = {
            "date":             datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "regime":           intel.get('regime_status', '?'),
            "vix":              intel.get('vix', 0),
            "breadth":          intel.get('breadth', 0),
            "geo":              intel.get('geo_status', '?'),
            "direction":        intel.get('direction_status', '?'),
            "candidates_total": len(all_signals),
            "qualified_count":  len(qualified),
            "blocked_count":    len(blocked_map),
            "best_signal":      best.get('name') if best else None,
            "best_type":        best.get('signal_type') if best else None,
            "best_score":       best.get('adjusted_score') if best else None,
            "signals":          signals_log,
            "data_provenance": {
                k: {"age_hours": v, "stale": v > 12}
                for k, v in _ages.items()
            },
        }

        # Keep last 90 days of runs (cap at 200 entries)
        log.append(run_record)
        if len(log) > 200:
            log = log[-200:]

        atomic_write(DECISION_LOG, log)
    except Exception as _e:
        log_error(f"Decision log write failed (non-fatal): {_e}")


# ============================================================
# MAIN — ORCHESTRATED DECISION PASS
# ============================================================

def run():
    # Session argument — 'am' (default, morning scan) or 'pm' (midday re-scan)
    _session = 'pm' if '--session=midday' in sys.argv else 'am'

    # Idempotency guard — one run per session per day (AM / PM)
    LAST_RUN_FILE = '/home/ubuntu/.picoclaw/logs/apex-engine-last-run.json'
    last_run  = safe_read(LAST_RUN_FILE, {})
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Also guard against duplicate fire within 5 minutes (cron double-trigger)
    last_ts = last_run.get('timestamp', '')
    if last_ts:
        try:
            lr_dt = datetime.fromisoformat(last_ts)
            if lr_dt.tzinfo is None:
                lr_dt = lr_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - lr_dt).total_seconds() < 300:
                print(f"Engine ran {int((datetime.now(timezone.utc) - lr_dt).total_seconds())}s ago — skipping duplicate")
                return
        except Exception:
            pass

    # Session-level guard: same session (am/pm) already ran today → skip
    last_session_date = last_run.get(f'last_{_session}_date', '')
    if last_session_date == today_str:
        print(f"Session '{_session}' already ran today ({today_str}) — skipping")
        return

    # Pre-flight data integrity check
    try:
        import importlib.util as _ilu_di
        _spec_di = _ilu_di.spec_from_file_location(
            "di", "/home/ubuntu/.picoclaw/scripts/apex-data-integrity.py")
        _di = _ilu_di.module_from_spec(_spec_di)
        _spec_di.loader.exec_module(_di)
        _di_ok, _di_fails = _di.quick_check()
        if not _di_ok:
            print(f"⚠️  Data integrity warnings: {len(_di_fails)} issues")
            for f in _di_fails[:3]:
                print(f"  → {f}")
            print("  Proceeding with caution — verify data manually")
        else:
            print("✅ Data integrity: CLEAR")
    except Exception as _di_e:
        print(f"⚠️  Data integrity check failed: {_di_e}")

    # ── Module cache — module-level global so scoring functions can access it ──
    global _MODULE_CACHE, _rs_mod, _mtf_mod
    _MODULE_CACHE.clear()  # fresh each run

    def _load_module(alias, filepath):
        """Load and cache a Python module."""
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

    # Pre-load all intelligence modules at startup
    SCRIPTS = '/home/ubuntu/.picoclaw/scripts'
    _regime_scaling = _load_module('rs',  f'{SCRIPTS}/apex-regime-scaling.py')
    _ev_calc        = _load_module('ev',  f'{SCRIPTS}/apex-expected-value.py')
    _mtf_mod        = _load_module('mtf', f'{SCRIPTS}/apex-multiframe.py')
    _rs_mod         = _load_module('rs2', f'{SCRIPTS}/apex-relative-strength.py')
    _bt_mod         = _load_module('bt',  f'{SCRIPTS}/apex-breadth-thrust.py')
    _atr_mod        = _load_module('atr', f'{SCRIPTS}/apex-atr-stops.py')
    _cg_mod         = _load_module('cg',  f'{SCRIPTS}/apex-contrarian-gates.py')
    _inv_mod        = _load_module('inv', f'{SCRIPTS}/apex-inverse-scanner.py')
    _macro_mod      = _load_module('macro', f'{SCRIPTS}/apex-macro-signals.py')

    print(f"  Modules loaded: {len(_MODULE_CACHE)}/9")

    now  = datetime.now(timezone.utc)
    date = now.strftime('%a %d %b %Y')

    print(f"🧠 APEX DECISION ENGINE — {date}", flush=True)
    print("="*50, flush=True)

    # Step 1 — Gather all intelligence
    print("\n[1/7] Gathering intelligence...", flush=True)
    intel = gather_intelligence()

    print(f"  Regime:    {intel['regime_status']} | VIX {intel['vix']} | Breadth {intel['breadth']}%")
    print(f"  Geo:       {intel['geo_status']}")
    print(f"  Direction: {intel['direction_status']}")
    print(f"  Drawdown:  {intel['drawdown_pct']}% ({intel['drawdown_status']})")
    print(f"  Sectors:   Leading={intel['leading_sectors']} | Lagging={intel['lagging_sectors']}")

    # Step 2 — Run all scanners
    print("\n[2/7] Running trend scan...", flush=True)
    trend_signals = run_trend_scan()
    print(f"  Found {len(trend_signals)} trend candidates")

    print("\n[3/7] Running contrarian scan...", flush=True)
    contrarian_signals = run_contrarian_scan(intel)
    print(f"  Found {len(contrarian_signals)} contrarian candidates")

    # Step 3 — Add drift and dividend signals
    print("\n[4/7] Checking drift and dividend signals...", flush=True)
    drift_signals    = intel['drift_signals']
    dividend_signals = intel['dividend_signals']
    print(f"  Drift: {len(drift_signals)} | Dividend: {len(dividend_signals)}")

    # Step 4 — Score all signals with full intelligence
    print("\n[5/7] Scoring with full intelligence...", flush=True)
    all_signals = []

    for s in trend_signals:
        s['signal_type'] = 'TREND'
        s['entry']  = s.get('price', 0)
        s['stop']   = round(s.get('price', 0) * (1 - 0.06), 2)
        scored = score_signal_with_intelligence(s, intel)
        all_signals.append(scored)

    for s in contrarian_signals:
        s['signal_type'] = 'CONTRARIAN'
        s['entry'] = s.get('price', 0)
        s['stop']  = round(s.get('price', 0) * 0.94, 2)

        # Run contrarian quality gates
        try:
            _cg_cached = _MODULE_CACHE.get('cg', _cg_mod)
            if _cg_cached:
                gate_result = _cg_cached.check_signal(s)
            else:
                gate_result = {'overall_pass': True, 'blocks': [], 'cautions': [], 'staged_entry': {}}
            if not gate_result.get('overall_pass', True):
                blocks = gate_result.get('blocks', [])
                print(f"  ❌ {s.get('name','?')} BLOCKED by gates: {blocks[0][:60] if blocks else 'unknown'}")
                continue  # Skip this signal
            # Attach staged entry plan to signal
            s['staged_entry'] = gate_result.get('staged_entry', {})
            s['gate_cautions'] = gate_result.get('cautions', [])
        except Exception as _e:
            log_error(f"Silent failure in apex-decision-engine.py: {_e}")  # Gates unavailable — proceed normally

        scored = score_signal_with_intelligence(s, intel)
        all_signals.append(scored)

    for s in drift_signals:
        s['signal_type'] = 'EARNINGS_DRIFT'
        scored = score_signal_with_intelligence(s, intel)
        all_signals.append(scored)

    for s in dividend_signals:
        s['signal_type'] = 'DIVIDEND_CAPTURE'
        scored = score_signal_with_intelligence(s, intel)
        all_signals.append(scored)

    # Inverse ETF signals
    try:
        _inv_cached = _MODULE_CACHE.get('inv', _inv_mod)
        if _inv_cached:
            inv_result = _inv_cached.run()
        else:
            inv_result = {'signals': []}
        inverse_signals = inv_result.get('signals', [])
        print(f"  Inverse ETF: {len(inverse_signals)} signals")
        for s in inverse_signals:
            s['signal_type'] = 'INVERSE'
            scored = score_signal_with_intelligence(s, intel)
            all_signals.append(scored)
    except Exception as e:
        print(f"  Inverse scan error: {e}")
        inverse_signals = []


    # Step 5 — Filter blocked signals
    print("\n[6/7] Filtering signals...", flush=True)
    qualified = []
    blocked_map = {}  # {name: [reasons]} — for decision log

    for signal in all_signals:
        blocks = is_blocked(signal, intel)
        if blocks:
            blocked_map[signal.get('name', '?')] = blocks
            print(f"  BLOCKED: {signal.get('name','?')} — {blocks[0]}")
        else:
            qualified.append(signal)

    print(f"  {len(qualified)} qualified | {len(blocked_map)} blocked")

    # Sort by adjusted score
    qualified.sort(key=lambda x: x.get('adjusted_score', 0), reverse=True)

    # Step 6 — Select best signal
    print("\n[7/7] Selecting best signal...", flush=True)

    if not qualified:
        # No signals — log the run then send defensive mode message
        log_decision_run(all_signals, blocked_map, qualified, None, intel)

        reason_parts = []
        if intel['regime_status'] == 'BLOCKED':
            reason_parts.append(f"Regime blocked (VIX {intel['vix']}, breadth {intel['breadth']}%)")
        if intel['geo_status'] == 'ALERT':
            reason_parts.append("Geo alert active")
        if intel['direction_status'] == 'BLOCKED':
            reason_parts.append("Market direction falling")

        reason = " | ".join(reason_parts) if reason_parts else "No qualifying signals"

        send_telegram(
            f"📊 APEX DECISION ENGINE — {date}\n\n"
            f"⚠️ DEFENSIVE MODE\n\n"
            f"Intelligence summary:\n"
            f"  VIX: {intel['vix']} | Breadth: {intel['breadth']}%\n"
            f"  Regime: {intel['regime_status']}\n"
            f"  Geo: {intel['geo_status']}\n"
            f"  Leading sectors: {', '.join(intel['leading_sectors']) or 'none'}\n\n"
            f"Reason: {reason}\n\n"
            f"Capital preserved. Next scan: 16:30."
        )
        print("  No qualifying signals — defensive mode")
        return

    best = qualified[0]
    name = best.get('name', '?')

    print(f"  Best signal: {best.get('name','?')} | Score: {best.get('adjusted_score',0)}/10 (raw:{best.get('raw_score',0)} | {best.get('confidence_pct',0)}% confidence) | Type: {best.get('signal_type','')}")

    # Print top 5 for reference
    print(f"\n  Top signals considered:")
    for i, s in enumerate(qualified[:5]):
        adj = s.get('adjusted_score', 0)
        base = s.get('total_score', s.get('contrarian_score', s.get('score', 0)))
        stype = s.get('signal_type', 'TREND')[:4]
        print(f"    {i+1}. {s.get('name','?'):8} | {stype} | base:{base} → {adj}/10 (raw:{s.get('raw_score',adj)} | {s.get('confidence_pct',0)}%) | RSI:{s.get('rsi',0)}")

    # Calculate final position size
    qty, notional = calculate_final_position(best, intel)

    # Calculate expected value
    entry  = float(best.get('entry', best.get('price', 0)))
    name   = best.get('name', '?')

    # ATR-based stops — replace fixed 6%
    try:
        _atr_cached = _MODULE_CACHE.get('atr', _atr_mod)
        if _atr_cached:
        # Get yahoo ticker from signal
            _yahoo = best.get('ticker', name)
            _atr_data = _atr_cached.get_full_atr_levels(
            name, _yahoo,
            signal_type=best.get('signal_type','TREND')
        )
        if _atr_data and _atr_data.get('stop', 0) > 0:
            stop = _atr_data['stop']
            t1   = _atr_data['target1']
            t2   = _atr_data['target2']
            best['atr_used'] = _atr_data['atr']
            best['stop_pct'] = _atr_data['stop_pct']
            print(f"  ATR stop: £{stop} ({_atr_data['stop_pct']}% | ATR £{_atr_data['atr']})")
        else:
            stop = float(best.get('stop', entry * 0.94))
            t1   = float(best.get('target1', entry + (entry-stop)*1.5))
            t2   = float(best.get('target2', entry + (entry-stop)*2.5))
    except Exception as _e:
        stop = float(best.get('stop', entry * 0.94))
        t1   = float(best.get('target1', entry + (entry-stop)*1.5))
        t2   = float(best.get('target2', entry + (entry-stop)*2.5))

    ev_mod = _MODULE_CACHE.get('ev', _ev_calc)

    ev_data = ev_mod.calculate_ev(entry, stop, t1, t2, best.get('signal_type'), qty)
    ev_mod.log_ev(best.get('name','?'), ev_data)

    best['ev']           = ev_data['ev']
    best['ev_verdict']   = ev_data['verdict']
    best['r_expectancy'] = ev_data['r_expectancy']
    best['breakeven_wr'] = ev_data['breakeven_wr']

    print(f"  EV: £{ev_data['ev']} ({ev_data['verdict']}) | R-expect: {ev_data['r_expectancy']} | Breakeven WR: {round(ev_data['breakeven_wr']*100,1)}%")

    # Option A — EV hard gate (only activates with 10+ real trades)
    if ev_data['ev'] < -5 and ev_data['sample_size'] >= 10:
        name = best.get('name', '?')
        blocked_map[name] = blocked_map.get(name, []) + [f"EV block: £{ev_data['ev']} negative EV ({ev_data['sample_size']} trades)"]
        log_decision_run(all_signals, blocked_map, qualified, None, intel)
        send_telegram(
            f"❌ EV BLOCK — {name}\n\n"
            f"Expected value: £{ev_data['ev']} (negative)\n"
            f"Based on {ev_data['sample_size']} real trades\n"
            f"R-expectancy: {ev_data['r_expectancy']}R\n\n"
            f"Signal skipped — negative expected value.\n"
            f"Capital preserved."
        )
        print(f"  EV BLOCKED: £{ev_data['ev']} negative EV on {ev_data['sample_size']} trades")
        return

    # Log this decision run (all candidates, scores, blocks, winner)
    log_decision_run(all_signals, blocked_map, qualified, best, intel)

    # Save and notify
    pending = save_and_notify(best, intel, qty, notional)
    print(f"\n  Signal saved: {pending.get('name')} | {qty} shares @ £{best.get('entry',0)}")

    # ── P2: Multi-signal queue — queue 2nd/3rd qualified signals ────────
    # Signals that score >= 7.0 are fully priced and queued for execution
    # at 09:30 UTC by apex-trade-queue.py execute, subject to all safety gates.
    try:
        import importlib.util as _ilu_tq
        _spec_tq = _ilu_tq.spec_from_file_location(
            "tq", "/home/ubuntu/.picoclaw/scripts/apex-trade-queue.py")
        _tq = _ilu_tq.module_from_spec(_spec_tq)
        _spec_tq.loader.exec_module(_tq)

        # Load ticker map once for t212_ticker resolution
        try:
            with open(TICKER_MAP) as _tf:
                _tmap = json.load(_tf)
        except Exception:
            _tmap = {}

        _queued_count = 0
        for _runner_up in qualified[1:3]:
            if _runner_up.get('adjusted_score', 0) < 7.0:
                continue
            if _runner_up.get('name') == best.get('name'):
                continue
            # Resolve T212 ticker if not already set
            if not _runner_up.get('t212_ticker'):
                _ru_name = _runner_up.get('name', '')
                _runner_up['t212_ticker'] = _tmap.get(_ru_name, {}).get('t212', '')
                if not _runner_up.get('sector'):
                    _runner_up['sector'] = _tmap.get(_ru_name, {}).get('sector', '')
            # Calculate position size for this secondary signal
            _r_qty, _r_notional = calculate_final_position(_runner_up, intel)
            if not _r_qty:
                continue
            # Enrich with EV
            _r_entry = float(_runner_up.get('entry', _runner_up.get('price', 0)))
            _r_stop  = float(_runner_up.get('stop', _r_entry * 0.94))
            _r_t1    = float(_runner_up.get('target1', _r_entry + (_r_entry - _r_stop) * 1.5))
            _r_t2    = float(_runner_up.get('target2', _r_entry + (_r_entry - _r_stop) * 2.5))
            try:
                _r_ev = ev_mod.calculate_ev(_r_entry, _r_stop, _r_t1, _r_t2,
                                             _runner_up.get('signal_type'), _r_qty)
                _runner_up['ev']       = _r_ev['ev']
                _runner_up['ev_verdict'] = _r_ev['verdict']
            except Exception:
                pass
            _runner_up['quantity'] = _r_qty
            _runner_up['notional'] = _r_notional
            _runner_up['entry']    = _r_entry
            _runner_up['stop']     = _r_stop
            _runner_up['target1']  = _r_t1
            _runner_up['target2']  = _r_t2
            _tq.add_scored_signal(_runner_up)
            _queued_count += 1
            print(f"  Queued runner-up: {_runner_up.get('name')} "
                  f"score={_runner_up.get('adjusted_score',0):.1f}")

        if _queued_count:
            print(f"  Multi-signal: {_queued_count} additional signal(s) queued for 09:30")
    except Exception as _tq_err:
        print(f"  Multi-signal queue skipped: {_tq_err}")

    # Run autopilot check
    subprocess.run(
        ['python3', '/home/ubuntu/.picoclaw/scripts/apex-autopilot.py', 'check'],
        capture_output=True, text=True
    )

    _now_iso = datetime.now(timezone.utc).isoformat()
    atomic_write(LAST_RUN_FILE, {
        **last_run,
        'timestamp':          _now_iso,
        f'last_{_session}_date': today_str,
    })
    print(f"\n✅ Decision engine complete (session={_session})")

if __name__ == '__main__':
    run()
