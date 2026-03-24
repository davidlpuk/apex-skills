#!/usr/bin/env python3
import json
import subprocess
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning, send_telegram, get_portfolio_value
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d if d is not None else {}
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')
    def send_telegram(m):
        print(f'TELEGRAM: {m[:80]}...')
    def get_portfolio_value(cache_max_age=300): return None

AUTOPILOT_FILE   = '/home/ubuntu/.picoclaw/logs/apex-autopilot.json'
SIGNAL_FILE      = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
CONTRARIAN_FILE  = '/home/ubuntu/.picoclaw/logs/apex-contrarian-signals.json'
POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
OUTCOMES_FILE    = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
PAUSE_FLAG       = '/home/ubuntu/.picoclaw/logs/apex-paused.flag'
GEO_FILE         = '/home/ubuntu/.picoclaw/logs/apex-geo-news.json'
DIRECTION_FILE   = '/home/ubuntu/.picoclaw/logs/apex-market-direction.json'
QUALITY_FILE     = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'
BREAKER_FILE     = '/home/ubuntu/.picoclaw/logs/apex-circuit-breaker.json'

def load_autopilot():
    try:
        with open(AUTOPILOT_FILE) as f:
            return json.load(f)
    except Exception:
        return {"enabled": False}

def save_autopilot(config):
    atomic_write(AUTOPILOT_FILE, config)

def load_signal():
    try:
        with open(SIGNAL_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def load_positions():
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def load_outcomes():
    try:
        with open(OUTCOMES_FILE) as f:
            return json.load(f)
    except Exception:
        return {"trades": [], "summary": {}}

def is_paused():
    return os.path.exists(PAUSE_FLAG)

def safety_check(config, signal):
    now   = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    blocks = []
    signal_type = signal.get('signal_type', 'TREND')

    # Max trades per day — stricter for contrarian
    max_trades = config.get('max_trades_per_day', 2)
    if signal_type == 'CONTRARIAN':
        max_trades = min(max_trades, 1)  # Only 1 contrarian trade per day
    if config.get('trades_today', 0) >= max_trades:
        blocks.append(f"Max {max_trades} {'contrarian ' if signal_type == 'CONTRARIAN' else ''}trades per day reached")

    # Daily loss limit
    loss_date  = config.get('daily_loss_date')
    daily_loss = config.get('daily_loss_today', 0) if loss_date == today else 0
    if daily_loss >= config.get('max_daily_loss', 100):
        blocks.append(f"Daily loss limit £{config['max_daily_loss']} reached")

    # Market hours
    if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
        blocks.append("No trades after 15:30 GMT")

    # Last 30 min of LSE session: institutional rebalancing widens spreads
    if now.hour == 15 and now.minute < 30:
        blocks.append("No entries in last 30 min of session (15:00–15:30 UTC — institutional close)")

    # Friday afternoon
    if now.weekday() == 4 and now.hour >= 12:
        blocks.append("No trades Friday afternoon")

    # Min time between trades
    last_trade = config.get('last_trade_time')
    if last_trade:
        try:
            last_dt = datetime.fromisoformat(last_trade)
            elapsed = (now - last_dt).total_seconds() / 3600
            min_hours = 4 if signal_type == 'CONTRARIAN' else 2
            if elapsed < min_hours:
                blocks.append(f"Min {min_hours}h between {'contrarian ' if signal_type == 'CONTRARIAN' else ''}trades — last was {round(elapsed,1)}h ago")
        except Exception as _e:
            log_error(f"Silent failure in apex-autopilot.py: {_e}")

    # Win rate check
    outcomes = load_outcomes()
    trades   = outcomes.get('trades', [])
    if len(trades) >= 10:
        win_rate = outcomes.get('summary', {}).get('win_rate', 50)
        if win_rate < 45:
            blocks.append(f"Win rate {win_rate}% too low (need 45%+ to cover costs and have edge)")

    # Losing streak defensive mode — reduce max trades when on a cold streak
    if len(trades) >= 3:
        last_3 = [t.get('pnl', 0) for t in trades[-3:]]
        if all(pnl < 0 for pnl in last_3):
            max_trades = min(max_trades, 1)
            if config.get('trades_today', 0) >= 1:
                blocks.append(f"Losing streak (last 3 trades negative) — max 1 trade/day in defensive mode")

    # Max open positions — contrarians allowed one extra slot on top of the configured limit
    positions = load_positions()
    has_contrarian = any(p.get('signal_type') == 'CONTRARIAN' for p in positions)
    base_max     = config.get('max_positions', 6)
    max_positions = base_max + 1 if (signal_type == 'CONTRARIAN' and not has_contrarian) else base_max
    if len(positions) >= max_positions:
        blocks.append(f"Max {max_positions} positions — have {len(positions)}")

    # Sector concentration limit — max 2 positions in the same sector.
    # Ticker-level correlation can pass at 0.65 while the portfolio is 100% sector-exposed
    # (e.g. XOM + CVX + SHEL are all energy, all correlated to oil price).
    # ETFs are excluded — they are diversified by nature.
    new_sector = signal.get('sector', 'UNKNOWN')
    if new_sector and new_sector not in ('UNKNOWN', 'ETF', 'unknown'):
        # Case-insensitive comparison — positions may have been written by older code
        same_sector_count = sum(
            1 for p in positions
            if p.get('sector', '').lower() == new_sector.lower()
            and p.get('sector', '').lower() not in ('etf', 'unknown', '')
        )
        if same_sector_count >= 2:
            blocks.append(f"Sector concentration: already {same_sector_count} positions in {new_sector} (max 2)")

    # Portfolio heat gate — block if total at-risk capital > 8% of portfolio
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "ph", "/home/ubuntu/.picoclaw/scripts/apex-portfolio-heat.py")
        _ph = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_ph)
        heat_mult, heat_status, heat_pct = _ph.get_heat_multiplier()
        if heat_status == 'CRITICAL':
            blocks.append(f"Portfolio heat {heat_pct:.1f}% exceeds 8% max — no new entries until risk reduces")
    except Exception as _e:
        log_error(f"Portfolio heat check failed in safety_check: {_e}")

    # Market microstructure: block first 30 min of US session (14:30–15:00 UTC)
    # Spreads are widest and fills are worst immediately after the US open.
    if now.hour == 14 and now.minute >= 30:
        blocks.append("No new entries during first 30 min of US session (14:30–15:00 UTC — wide spreads)")

    # Pre-market futures gap gate — if S&P futures gapped down >1.5%, block TREND entries
    futures_gap_flag = '/home/ubuntu/.picoclaw/logs/apex-futures-gap.flag'
    if signal_type != 'CONTRARIAN' and os.path.exists(futures_gap_flag):
        try:
            with open(futures_gap_flag) as _fg:
                gap_content = _fg.read().strip()
            blocks.append(f"Pre-market gap down flag active ({gap_content}) — TREND entries suppressed")
        except Exception:
            blocks.append("Pre-market gap down flag active — TREND entries suppressed")

    return blocks

def realtime_correlation_check(signal):
    """Check new signal against existing positions in real time."""
    name   = signal.get('name', '')
    ticker = signal.get('t212_ticker', '')
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "rtc", "/home/ubuntu/.picoclaw/scripts/apex-realtime-correlation.py")
        _rtc = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_rtc)

        # Get yahoo ticker
        from apex_utils import get_yahoo_ticker
        yahoo = get_yahoo_ticker(ticker) or name

        blocked, max_corr, high = _rtc.check_new_position_correlation(ticker, yahoo)

        if blocked:
            corr_str = ", ".join([f"{h['position']} ({h['corr']:+.2f})" for h in high[:2]])
            return "BLOCK", [f"Correlation {max_corr:.2f} with {corr_str}"]
        return "CLEAR", []
    except Exception as e:
        return "CLEAR", []

def staleness_check():
    result = subprocess.run(
        ['/home/ubuntu/bin/python3', '/home/ubuntu/.picoclaw/scripts/apex-staleness-check.py'],
        capture_output=True, text=True
    )
    output = result.stdout.strip()
    first  = output.split('\n')[0] if output else ''
    status = first.split('|')[0] if '|' in first else 'UNKNOWN'
    reason = first.split('|')[2] if len(first.split('|')) > 2 else first
    return status, reason

def geo_news_check(signal):
    signal_type = signal.get('signal_type', 'TREND')
    try:
        with open(GEO_FILE) as f:
            data = json.load(f)
    except Exception:
        return "CLEAR", []

    overall = data.get('overall', 'CLEAR')
    if overall != 'ALERT':
        return overall, []

    try:
        with open(QUALITY_FILE) as f:
            quality_db = json.load(f)
        energy_favs = quality_db.get('geo_event_map', {}).get('iran_war', {}).get('favour', [])
    except Exception:
        energy_favs = ['XOM','CVX','SHEL','BP','TTE','IUES']

    name   = signal.get('name', '')
    sector = signal.get('sector', '')

    # Contrarian trades allowed during geo alert — buying the panic dip
    if signal_type in ['CONTRARIAN', 'GEO_REVERSAL']:
        return "CLEAR", []

    # All TREND/momentum entries blocked during geo ALERT — uncertainty too high
    return "BLOCK", ["Geo alert active — all trend entries halted (contrarian still allowed)"]

def market_direction_check(signal):
    signal_type = signal.get('signal_type', 'TREND')

    # Contrarian trades ignore market direction — that's the whole point
    if signal_type in ['CONTRARIAN', 'GEO_REVERSAL']:
        return "CLEAR", []

    try:
        with open(DIRECTION_FILE) as f:
            data = json.load(f)
        return data.get('overall', 'CLEAR'), data.get('blocks', [])
    except Exception:
        return 'CLEAR', []

def regime_check(signal):
    signal_type = signal.get('signal_type', 'TREND')

    # Contrarian trades work during regime blocks — skip this check
    if signal_type in ['CONTRARIAN', 'GEO_REVERSAL']:
        return "CLEAR", []

    try:
        import subprocess
        result = subprocess.run(
            ['/home/ubuntu/bin/python3', '/home/ubuntu/.picoclaw/scripts/apex-regime-check.py'],
            capture_output=True, text=True
        )
        output = result.stdout
        start  = output.find('=== JSON ===')
        if start == -1:
            return "CLEAR", []
        data   = json.loads(output[start + len('=== JSON ==='):].strip())
        overall = data.get('overall', 'CLEAR')
        reasons = data.get('block_reason', [])
        return overall, reasons
    except Exception:
        return "CLEAR", []

def contrarian_quality_check(signal):
    signal_type = signal.get('signal_type', 'TREND')
    if signal_type != 'CONTRARIAN':
        return "CLEAR", []

    name = signal.get('name', '')
    rsi  = float(signal.get('rsi', 50))

    # Hard rule — contrarian trades only on quality names
    try:
        with open(QUALITY_FILE) as f:
            quality_db = json.load(f)
        quality = quality_db.get('quality_stocks', {})
        if name not in quality:
            return "BLOCK", [f"{name} not in quality universe — contrarian trades quality only"]
        qs = quality[name].get('quality_score', 0)
        if qs < 7:
            return "BLOCK", [f"{name} quality score {qs}/10 too low for contrarian trade"]
    except Exception as _e:
        log_error(f"Silent failure in apex-autopilot.py: {_e}")

    # RSI must be genuinely oversold for contrarian — RSI 38 is just "slightly weak",
    # not oversold. Professional contrarian entries need real capitulation (RSI < 30).
    if rsi > 30:
        return "BLOCK", [f"RSI {rsi} not oversold enough for contrarian trade (need < 30)"]

    return "CLEAR", []

def check_intraday_signal_decay(signal):
    """
    Intraday score decay: re-evaluate whether the signal still has edge
    at current price vs the price when it was generated (signal_generated_at).

    A TREND signal generated at 08:30 at £100 that now trades at £103 at
    10:30 has already moved 3% — less upside remains, more risk that it
    is now extended. Score decay prevents chasing.

    Returns (ok, reason, effective_score)
    """
    import yfinance as yf

    signal_price    = float(signal.get('entry', 0))
    original_score  = float(signal.get('score', signal.get('contrarian_score', 7.5)))
    sig_type        = signal.get('signal_type', 'TREND')
    threshold       = float(signal.get('score_threshold', 7.0))
    name            = signal.get('name', '')
    t212_ticker     = signal.get('t212_ticker', '')

    if signal_price <= 0:
        return True, "No signal price — decay check skipped", original_score

    # Only apply decay if signal is at least 45 minutes old
    generated_at = signal.get('generated_at', signal.get('created_at', ''))
    if generated_at:
        try:
            gen_dt = datetime.fromisoformat(generated_at)
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 60
            if age_min < 45:
                return True, f"Signal only {age_min:.0f} min old — decay check skipped", original_score
        except Exception:
            pass

    # Fetch current price via yfinance
    YAHOO_MAP = {
        "VUAGl_EQ": "VUAG.L", "XOM_US_EQ": "XOM", "V_US_EQ": "V",
        "AAPL_US_EQ": "AAPL", "MSFT_US_EQ": "MSFT", "NVDA_US_EQ": "NVDA",
        "GOOGL_US_EQ": "GOOGL", "JPM_US_EQ": "JPM", "CVX_US_EQ": "CVX",
        "ABBV_US_EQ": "ABBV", "JNJ_US_EQ": "JNJ", "GS_US_EQ": "GS",
        "SHEL_EQ": "SHEL.L", "HSBA_EQ": "HSBA.L", "AZN_EQ": "AZN.L",
        "QQQSl_EQ": "QQQS.L", "3USSl_EQ": "3USS.L", "SQQQ_EQ": "SQQQ",
    }
    yahoo = YAHOO_MAP.get(t212_ticker, '')
    if not yahoo:
        return True, "No Yahoo ticker — decay check skipped", original_score

    try:
        hist = yf.Ticker(yahoo).history(period="1d", interval="5m")
        if hist.empty:
            return True, "No price data — decay check skipped", original_score
        current_price = float(hist['Close'].iloc[-1])
        if yahoo.endswith('.L') and current_price > 100:
            current_price /= 100
    except Exception as e:
        log_error(f"Decay price fetch failed for {name}: {e}")
        return True, "Price fetch failed — decay check skipped", original_score

    drift_pct = (current_price - signal_price) / signal_price * 100

    # Apply same decay logic as queue-revalidate
    if sig_type == 'TREND':
        free_buffer = 1.0
        decay_rate  = 0.5
        adverse     = max(drift_pct - free_buffer, 0)
    elif sig_type == 'CONTRARIAN':
        free_buffer = -2.0
        decay_rate  = 0.4
        adverse     = max(free_buffer - drift_pct, 0)
    else:
        free_buffer = 2.0
        decay_rate  = 0.3
        adverse     = max(abs(drift_pct) - free_buffer, 0)

    score_loss    = round(adverse * decay_rate, 2)
    effective     = round(original_score - score_loss, 2)

    if effective < threshold:
        return (False,
                f"Score decayed {original_score}→{effective} (drift {drift_pct:+.1f}%, "
                f"loss {score_loss:.1f}pts) — below threshold {threshold}",
                effective)

    if score_loss > 0:
        return (True,
                f"Score mild decay {original_score}→{effective} (drift {drift_pct:+.1f}%)",
                effective)

    return True, f"Score intact {original_score} (drift {drift_pct:+.1f}%)", original_score


def get_dynamic_position_size(signal):
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-regime.json') as f:
            regime = json.load(f)
        vix     = float(regime.get('vix', 20))
        breadth = float(regime.get('breadth_pct', 50))
    except Exception:
        vix, breadth = 20, 50

    try:
        with open(QUALITY_FILE) as f:
            quality_db = json.load(f)
        qs = quality_db.get('quality_stocks', {}).get(signal.get('name', ''), {}).get('quality_score', 5)
    except Exception:
        qs = 5

    try:
        from apex_position_sizer import calculate_position
        from apex_utils import get_portfolio_value
        result = calculate_position(
            portfolio_value=get_portfolio_value() or 5000,
            entry_price=float(signal.get('entry', 100)),
            stop_price=float(signal.get('stop', 90)),
            signal_score=float(signal.get('score', signal.get('contrarian_score', 7))),
            max_score=10,
            signal_type=signal.get('signal_type', 'TREND'),
            vix=vix,
            breadth=breadth,
            quality_score=qs,
            currency=signal.get('currency', 'GBP'),
        )
        return result
    except Exception as e:
        log_error(f"get_dynamic_position_size failed: {e}")
        return None

def execute_autonomously():
    result = subprocess.run(
        ['bash', '/home/ubuntu/.picoclaw/scripts/apex-execute-order.sh'],
        capture_output=True, text=True
    )
    return result.returncode == 0

def run(mode='check'):
    config = load_autopilot()
    now    = datetime.now(timezone.utc)
    today  = now.strftime('%Y-%m-%d')

    # Reset daily counters at market session open (07:00 UTC covers GMT and BST market open),
    # not at midnight. Without this the limit reset 8 hours before the market opened,
    # allowing a fresh batch of trades at 00:01 UTC on the same calendar trading day.
    market_open_utc = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= market_open_utc:
        session_date = now.strftime('%Y-%m-%d')
    else:
        session_date = (now - timedelta(days=1)).strftime('%Y-%m-%d')

    if config.get('daily_loss_date') != session_date:
        config['trades_today']     = 0
        config['daily_loss_today'] = 0
        config['daily_loss_date']  = session_date
        save_autopilot(config)

    if mode == 'status':
        status = "🤖 AUTOPILOT ON" if config.get('enabled') else "👤 MANUAL MODE"
        paused = " (PAUSED)" if is_paused() else ""
        print(f"{status}{paused}")
        print(f"Trades today: {config.get('trades_today', 0)}/{config.get('max_trades_per_day', 2)}")
        print(f"Daily loss: £{config.get('daily_loss_today', 0)}/{config.get('max_daily_loss', 100)}")
        print(f"Total autonomous trades: {config.get('total_autonomous_trades', 0)}")
        return

    if mode == 'on':
        config['enabled']      = True
        config['activated_at'] = now.isoformat()
        save_autopilot(config)
        max_pos = config.get('max_positions', 6)
        max_tr  = config.get('max_trades_per_day', 3)
        send_telegram(f"🤖 AUTOPILOT ACTIVATED — DUAL MODE\n\nApex now trades both:\n📈 Trend signals — momentum trades\n🔄 Contrarian signals — quality at discount\n\nSafety limits:\n• Max {max_tr} trades per day\n• Max {max_pos} positions ({max_pos + 1} if one is contrarian)\n• Contrarian: quality universe only, RSI < 30\n• No trades after 15:30 GMT\n• No Friday afternoon\n• Min 4h between contrarian trades\n• Exchange-level stop loss on every trade\n\nType AUTOPILOT OFF anytime.")
        print("Autopilot ON — dual mode")
        return

    if mode == 'off':
        config['enabled'] = False
        save_autopilot(config)
        send_telegram("👤 MANUAL MODE RESTORED")
        print("Autopilot OFF")
        return

    if not config.get('enabled'):
        print("MANUAL — waiting for CONFIRM")
        return

    if is_paused():
        print("PAUSED")
        return

    signal = load_signal()
    if not signal:
        print("No pending signal")
        return

    name        = signal.get('name', '?')
    entry       = signal.get('entry', 0)
    stop        = signal.get('stop', 0)
    score       = signal.get('score', signal.get('contrarian_score', 0))
    ticker      = signal.get('t212_ticker', '?')
    signal_type = signal.get('signal_type', 'TREND')

    type_icon = "🔄" if signal_type == 'CONTRARIAN' else "📈"
    type_label = "CONTRARIAN" if signal_type == 'CONTRARIAN' else "TREND"

    # Intraday signal decay — re-score at current price before executing
    decay_ok, decay_msg, effective_score = check_intraday_signal_decay(signal)
    print(f"  Signal decay: {decay_msg}")
    if not decay_ok:
        send_telegram(
            f"📉 SIGNAL DECAY BLOCK\n\n{type_icon} {name} ({type_label})\n{decay_msg}\n\n"
            f"Signal will be re-evaluated at next morning scan."
        )
        print(f"DECAY BLOCKED: {decay_msg}")
        return
    if effective_score != score:
        signal['score_at_execution'] = effective_score

    # Safety checks
    blocks = safety_check(config, signal)
    if blocks:
        reason = "\n• ".join(blocks)
        send_telegram(f"🤖 AUTOPILOT BLOCKED\n\n{type_icon} {name} ({type_label})\n• {reason}")
        print(f"BLOCKED: {reason}")
        return

    # Contrarian quality check
    if signal_type == 'CONTRARIAN':
        q_status, q_blocks = contrarian_quality_check(signal)
        if q_status == "BLOCK":
            reason = " | ".join(q_blocks)
            send_telegram(f"🤖 QUALITY BLOCK\n\n{name}\n{reason}")
            print(f"QUALITY BLOCKED: {reason}")
            return

    # EDGAR insider hard-block — if cluster selling, require score >= 8
    try:
        import json as _json
        from pathlib import Path as _Path
        _sig_file = _Path('/home/ubuntu/.picoclaw/data/apex-insider-signal.json')
        if _sig_file.exists():
            _insider_data = _json.loads(_sig_file.read_text())
            _insider_score = _insider_data.get('signals', {}).get(name, {}).get('score', 0)
            _insider_reasons = _insider_data.get('signals', {}).get(name, {}).get('reasons', [])
            if _insider_score <= -2 and score < 8:
                _reason = ' | '.join(_insider_reasons)
                send_telegram(f"🔴 INSIDER BLOCK\n\n{type_icon} {name}\nCluster insider selling detected\n{_reason}\nScore {score}/10 below 8.0 threshold required to override\n\nTrade blocked.")
                print(f"INSIDER BLOCKED: score={score} < 8 required | {_reason}")
                return
            elif _insider_score <= -2:
                print(f"  Insider warning: {_insider_score} ({_reason}) — score {score} clears threshold")
    except Exception as _e:
        log_error(f"EDGAR insider block check failed: {_e}")

    # Regime check — skipped for contrarian
    reg_status, reg_blocks = regime_check(signal)
    if reg_status == "BLOCKED":
        reason = " | ".join(reg_blocks)
        send_telegram(f"🤖 REGIME BLOCK\n\n{name} ({type_label})\n{reason}")
        print(f"REGIME BLOCKED: {reason}")
        return

    # ── Four Pillars Audit ─────────────────────────────────────
    try:
        import importlib.util as _ilu_bs
        _spec_bs = _ilu_bs.spec_from_file_location(
            "bs", "/home/ubuntu/.picoclaw/scripts/apex-blackswan-test.py")
        _bs = _ilu_bs.module_from_spec(_spec_bs)
        _spec_bs.loader.exec_module(_bs)
        bs_ok, bs_msg = _bs.pre_trade_check(signal)
        if not bs_ok:
            send_telegram(f"🦢 BLACK SWAN BLOCK\n\n{name}\n{bs_msg}")
            print(f"BLACK SWAN BLOCKED: {bs_msg}")
            return
        elif 'CAUTION' in bs_msg:
            print(f"Black Swan caution: {bs_msg}")
    except Exception as _e:
        log_error(f"Black swan check failed: {_e}")

    # Simons noise check
    try:
        import importlib.util as _ilu_si
        _spec_si = _ilu_si.spec_from_file_location(
            "si", "/home/ubuntu/.picoclaw/scripts/apex-simons-test.py")
        _si = _ilu_si.module_from_spec(_spec_si)
        _spec_si.loader.exec_module(_si)
        _regime = intel.get('regime_label', 'NEUTRAL') if hasattr(signal, 'get') else 'NEUTRAL'
        noise, regime_wr, is_sig, simons_rec = _si.audit_signal(signal, _regime)
        print(f"  Simons: noise={noise}/10 | {simons_rec[:60]}")
        if noise >= 9 and is_sig:
            send_telegram(f"📊 SIMONS WARNING\n\n{name}\nNoise score {noise}/10\n{simons_rec}")
    except Exception as _e:
        log_error(f"Simons check failed: {_e}")

    # Thorp Kelly check
    try:
        import importlib.util as _ilu_th
        _spec_th = _ilu_th.spec_from_file_location(
            "th", "/home/ubuntu/.picoclaw/scripts/apex-thorp-test.py")
        _th = _ilu_th.module_from_spec(_spec_th)
        _spec_th.loader.exec_module(_th)
        thorp_result = _th.audit_signal(signal)
        if thorp_result:
            print(f"  Thorp: Kelly half=£{thorp_result.get('kelly_half_risk',0)} | {thorp_result.get('verdict','')}")
            if thorp_result.get('verdict') == 'ABORT':
                send_telegram(f"📐 THORP ABORT\n\n{name}\nNegative Kelly — no mathematical edge")
                return
    except Exception as _e:
        log_error(f"Thorp check failed: {_e}")

    # Shaw liquidity check
    try:
        import importlib.util as _ilu_sh
        _spec_sh = _ilu_sh.spec_from_file_location(
            "sh", "/home/ubuntu/.picoclaw/scripts/apex-shaw-test.py")
        _sh = _ilu_sh.module_from_spec(_spec_sh)
        _spec_sh.loader.exec_module(_sh)
        shaw_result = _sh.audit_signal_liquidity(signal)
        if shaw_result:
            print(f"  Shaw: est. cost=£{shaw_result.get('round_trip_cost_gbp',0)} | {shaw_result.get('verdict','')}")
    except Exception as _e:
        log_error(f"Shaw check failed: {_e}")

    # Bid-ask spread gate — block if spread > 0.80%
    try:
        import importlib.util as _ilu_sp
        _spec_sp = _ilu_sp.spec_from_file_location(
            "sp", "/home/ubuntu/.picoclaw/scripts/apex-spread-check.py")
        _sp = _ilu_sp.module_from_spec(_spec_sp)
        _spec_sp.loader.exec_module(_sp)
        _sp_verdict, _sp_pct, _sp_mid, _sp_details = _sp.check_spread(signal)
        print(f"  Spread: {_sp_pct:.4f}% → {_sp_verdict}")
        if _sp_verdict == 'BLOCK':
            send_telegram(f"🔴 SPREAD BLOCK\n\n{name}\nSpread {_sp_pct:.3f}% > 0.80% — skipping trade")
            print(f"SPREAD BLOCKED: {_sp_pct:.3f}%")
            return
        # Use mid-price as limit price if spread data is available
        if _sp_verdict in ('NORMAL', 'WIDE') and _sp_mid > 0:
            signal['limit_price'] = _sp_mid
    except Exception as _e:
        log_error(f"Spread check failed: {_e}")

    # ── End Four Pillars ────────────────────────────────────────

    # Safe haven flow check — block if crisis-level flight-to-safety detected
    try:
        import importlib.util as _ilu_sh2
        _spec_sh2 = _ilu_sh2.spec_from_file_location(
            "shv", "/home/ubuntu/.picoclaw/scripts/apex-safe-haven.py")
        _shv = _ilu_sh2.module_from_spec(_spec_sh2)
        _spec_sh2.loader.exec_module(_shv)
        _shv_score, _shv_level = _shv.get_safe_haven_score()
        if _shv_score >= 9:  # CRISIS — halt all entries
            send_telegram(
                f"🔴 SAFE HAVEN CRISIS BLOCK\n\n{name}\n"
                f"Safe haven score {_shv_score}/12 ({_shv_level})\n"
                f"Flight-to-safety detected — halting new entries"
            )
            print(f"SAFE HAVEN BLOCKED: score={_shv_score} level={_shv_level}")
            return
        elif _shv_score >= 6:  # WARNING — log but allow CONTRARIAN
            if signal.get('signal_type', 'TREND') != 'CONTRARIAN':
                send_telegram(
                    f"🟠 SAFE HAVEN WARNING BLOCK\n\n{name}\n"
                    f"Safe haven score {_shv_score}/12 ({_shv_level})\n"
                    f"Significant flight-to-safety — halting trend entries"
                )
                print(f"SAFE HAVEN WARNING BLOCK: score={_shv_score}")
                return
    except Exception as _e:
        log_error(f"Safe haven check failed: {_e}")

    # Real-time correlation check
    corr_status, corr_blocks = realtime_correlation_check(signal)
    if corr_status == "BLOCK":
        reason = " | ".join(corr_blocks)
        send_telegram(f"🔗 CORRELATION BLOCK\n\n{name}\n{reason}\n\nToo correlated with existing position.")
        print(f"CORRELATION BLOCKED: {reason}")
        return

    # Staleness check
    stale_status, stale_reason = staleness_check()
    if stale_status == 'ABORT':
        send_telegram(f"🤖 SIGNAL STALE\n\n{name}\n{stale_reason}")
        print(f"STALE: {stale_reason}")
        return

    # Geo check — energy favoured during conflict
    geo_status, geo_blocks = geo_news_check(signal)
    if geo_status == "BLOCK":
        reason = " | ".join(geo_blocks)
        send_telegram(f"🌍 GEO BLOCK\n\n{name}\n{reason}")
        print(f"GEO BLOCKED: {reason}")
        return

    # Market direction — skipped for contrarian
    dir_status, dir_blocks = market_direction_check(signal)
    if dir_status == "BLOCKED":
        reason = " | ".join(dir_blocks)
        send_telegram(f"📉 DIRECTION BLOCK\n\n{name}\n{reason}")
        print(f"DIRECTION BLOCKED: {reason}")
        return

    # All checks passed — execute
    rsi     = signal.get('rsi', 0)
    reasons = signal.get('reasons', [])
    reason_str = ' | '.join(reasons[:2]) if reasons else ''

    if signal_type == 'CONTRARIAN':
        send_telegram(f"🤖 AUTOPILOT EXECUTING — CONTRARIAN\n\n🔄 {name}\nRSI: {rsi} (deeply oversold)\nEntry: £{entry} | Stop: £{stop}\nC-Score: {score}/10\n{reason_str}\n\nBuying quality at discount...")
    else:
        send_telegram(f"🤖 AUTOPILOT EXECUTING — TREND\n\n📈 {name}\nEntry: £{entry} | Stop: £{stop}\nScore: {score}/10\n\nPlacing orders now...")

    success = execute_autonomously()

    if success:
        config['trades_today']            = config.get('trades_today', 0) + 1
        config['total_autonomous_trades'] = config.get('total_autonomous_trades', 0) + 1
        config['last_trade_time']         = now.isoformat()

        # Decrement recovery ramp if active (post-SUSPEND 50% sizing period)
        try:
            _cb_data = safe_read(BREAKER_FILE, {})
            if _cb_data.get('recovery_trades_remaining', 0) > 0:
                _cb_data['recovery_trades_remaining'] -= 1
                atomic_write(BREAKER_FILE, _cb_data)
                ramp_left = _cb_data['recovery_trades_remaining']
                print(f"  Recovery ramp: {ramp_left} trades remaining at 50% sizing")
        except Exception as _e:
            log_error(f"Recovery ramp decrement failed: {_e}")

        config.setdefault('log', []).append({
            "time":        now.isoformat(),
            "action":      "AUTO_EXECUTE",
            "signal_type": signal_type,
            "name":        name,
            "ticker":      ticker,
            "entry":       entry,
            "stop":        stop,
            "score":       score
        })
        save_autopilot(config)

        if signal_type == 'CONTRARIAN':
            send_telegram(f"🤖 CONTRARIAN TRADE COMPLETE\n\n✅ {name} purchased at discount\nEntry: £{entry} | Stop: £{stop} (in T212)\nRSI was {rsi} — mean reversion play\nC-Score: {score}/10\n\nApex will monitor for return to 50-day EMA.\nType AUTOPILOT OFF anytime.")
        else:
            send_telegram(f"🤖 TREND TRADE COMPLETE\n\n✅ {name} purchased\nEntry: £{entry} | Stop: £{stop} (in T212)\nScore: {score}/10\n\nType AUTOPILOT OFF anytime.")
    else:
        send_telegram(f"🤖 EXECUTION FAILED\n\n{name} order failed — check logs.")

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'check'
    run(mode)
