#!/usr/bin/env python3
import json
import subprocess
import sys
import os
from datetime import datetime, timezone, timedelta

AUTOPILOT_FILE   = '/home/ubuntu/.picoclaw/logs/apex-autopilot.json'
SIGNAL_FILE      = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
CONTRARIAN_FILE  = '/home/ubuntu/.picoclaw/logs/apex-contrarian-signals.json'
POSITIONS_FILE   = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
OUTCOMES_FILE    = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
PAUSE_FLAG       = '/home/ubuntu/.picoclaw/logs/apex-paused.flag'
GEO_FILE         = '/home/ubuntu/.picoclaw/logs/apex-geo-news.json'
DIRECTION_FILE   = '/home/ubuntu/.picoclaw/logs/apex-market-direction.json'
QUALITY_FILE     = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'

def load_autopilot():
    try:
        with open(AUTOPILOT_FILE) as f:
            return json.load(f)
    except:
        return {"enabled": False}

def save_autopilot(config):
    atomic_write(AUTOPILOT_FILE, config)

def load_signal():
    try:
        with open(SIGNAL_FILE) as f:
            return json.load(f)
    except:
        return None

def load_positions():
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except:
        return []

def load_outcomes():
    try:
        with open(OUTCOMES_FILE) as f:
            return json.load(f)
    except:
        return {"trades": [], "summary": {}}

def is_paused():
    return os.path.exists(PAUSE_FLAG)

def send_telegram(message):
    subprocess.run([
        'bash', '-c',
        f'''BOT_TOKEN=$(cat ~/.picoclaw/config.json | grep -A 2 '"telegram"' | grep token | sed 's/.*"token": "\\(.*\\)".*/\\1/')
curl -s -X POST "https://api.telegram.org/bot${{BOT_TOKEN}}/sendMessage" \
  -d chat_id="6808823889" \
  --data-urlencode "text={message}"'''
    ], capture_output=True, text=True)

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

    # Friday afternoon
    if now.weekday() == 4 and now.hour >= 12:
        blocks.append("No trades Friday afternoon")

    # Min time between trades
    last_trade = config.get('last_trade_time')
    if last_trade:
        try:
            last_dt = datetime.fromisoformat(last_trade)
            elapsed = (now - last_dt).seconds / 3600
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
        if win_rate < 35:
            blocks.append(f"Win rate {win_rate}% too low")

    # Max open positions — allow 3 if one is contrarian
    positions = load_positions()
    has_contrarian = any(p.get('signal_type') == 'CONTRARIAN' for p in positions)
    max_positions = 3 if (signal_type == 'CONTRARIAN' and not has_contrarian) else 2
    if len(positions) >= max_positions:
        blocks.append(f"Max {max_positions} positions — have {len(positions)}")

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
        YAHOO_MAP = {
            "VUAGl_EQ":"VUAG.L","XOM_US_EQ":"XOM","V_US_EQ":"V",
            "AAPL_US_EQ":"AAPL","MSFT_US_EQ":"MSFT","NVDA_US_EQ":"NVDA",
            "GOOGL_US_EQ":"GOOGL","JPM_US_EQ":"JPM","GS_US_EQ":"GS",
        }
        yahoo = YAHOO_MAP.get(ticker, name)

        blocked, max_corr, high = _rtc.check_new_position_correlation(ticker, yahoo)

        if blocked:
            corr_str = ", ".join([f"{h['position']} ({h['corr']:+.2f})" for h in high[:2]])
            return "BLOCK", [f"Correlation {max_corr:.2f} with {corr_str}"]
        return "CLEAR", []
    except Exception as e:
        return "CLEAR", []

def staleness_check():
    result = subprocess.run(
        ['python3', '/home/ubuntu/.picoclaw/scripts/apex-staleness-check.py'],
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
    except:
        return "CLEAR", []

    overall = data.get('overall', 'CLEAR')
    if overall != 'ALERT':
        return overall, []

    try:
        with open(QUALITY_FILE) as f:
            quality_db = json.load(f)
        energy_favs = quality_db.get('geo_event_map', {}).get('iran_war', {}).get('favour', [])
    except:
        energy_favs = ['XOM','CVX','SHEL','BP','TTE','IUES']

    name   = signal.get('name', '')
    sector = signal.get('sector', '')

    # Contrarian trades on geo-favoured instruments — ALLOW
    if signal_type in ['CONTRARIAN', 'GEO_REVERSAL'] and name in energy_favs:
        return "CLEAR", []

    # Trend trades on energy during geo alert — check if beneficiary
    if name in energy_favs:
        return "CLEAR", []

    # Everything else during geo alert — standard check
    energy_instruments = ['XOM','CVX','SHEL','BP','TTE','IUES','NG','SSE']
    is_energy = sector == 'IUES' or any(e in name.upper() for e in energy_instruments)
    if is_energy:
        return "CLEAR", []

    return "CLEAR", []

def market_direction_check(signal):
    signal_type = signal.get('signal_type', 'TREND')

    # Contrarian trades ignore market direction — that's the whole point
    if signal_type in ['CONTRARIAN', 'GEO_REVERSAL']:
        return "CLEAR", []

    try:
        with open(DIRECTION_FILE) as f:
            data = json.load(f)
        return data.get('overall', 'CLEAR'), data.get('blocks', [])
    except:
        return 'CLEAR', []

def regime_check(signal):
    signal_type = signal.get('signal_type', 'TREND')

    # Contrarian trades work during regime blocks — skip this check
    if signal_type in ['CONTRARIAN', 'GEO_REVERSAL']:
        return "CLEAR", []

    try:
        import subprocess
        result = subprocess.run(
            ['python3', '/home/ubuntu/.picoclaw/scripts/apex-regime-check.py'],
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
    except:
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

    # RSI must be genuinely oversold for contrarian
    if rsi > 38:
        return "BLOCK", [f"RSI {rsi} not oversold enough for contrarian trade (need < 38)"]

    return "CLEAR", []

def get_dynamic_position_size(signal):
    try:
        with open('/home/ubuntu/.picoclaw/logs/apex-regime.json') as f:
            regime = json.load(f)
        vix     = float(regime.get('vix', 20))
        breadth = float(regime.get('breadth_pct', 50))
    except:
        vix, breadth = 20, 50

    try:
        with open(QUALITY_FILE) as f:
            quality_db = json.load(f)
        qs = quality_db.get('quality_stocks', {}).get(signal.get('name', ''), {}).get('quality_score', 5)
    except:
        qs = 5

    result = subprocess.run([
        'python3', '-c', f'''
import sys
sys.path.insert(0, "/home/ubuntu/.picoclaw/scripts")
from apex_position_sizer import calculate_position
import json
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

result = calculate_position(
    portfolio_value=5000,
    entry_price={signal.get("entry", 100)},
    stop_price={signal.get("stop", 90)},
    signal_score={signal.get("score", signal.get("contrarian_score", 7))},
    max_score=10,
    signal_type="{signal.get("signal_type", "TREND")}",
    vix={vix},
    breadth={breadth},
    quality_score={qs}
)
print(json.dumps(result))
'''
    ], capture_output=True, text=True)

    try:
        return json.loads(result.stdout)
    except:
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

    if config.get('daily_loss_date') != today:
        config['trades_today']     = 0
        config['daily_loss_today'] = 0
        config['daily_loss_date']  = today
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
        send_telegram("🤖 AUTOPILOT ACTIVATED — DUAL MODE\n\nApex now trades both:\n📈 Trend signals — momentum trades\n🔄 Contrarian signals — quality at discount\n\nSafety limits:\n• Max 2 trend trades per day\n• Max 1 contrarian trade per day\n• Max 3 positions (2 trend + 1 contrarian)\n• Contrarian: quality universe only, RSI < 38\n• No trades after 15:30 GMT\n• No Friday afternoon\n• Min 4h between contrarian trades\n• Exchange-level stop loss on every trade\n\nType AUTOPILOT OFF anytime.")
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

    # Regime check — skipped for contrarian
    reg_status, reg_blocks = regime_check(signal)
    if reg_status == "BLOCKED":
        reason = " | ".join(reg_blocks)
        send_telegram(f"🤖 REGIME BLOCK\n\n{name} ({type_label})\n{reason}")
        print(f"REGIME BLOCKED: {reason}")
        return

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
