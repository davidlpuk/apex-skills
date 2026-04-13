#!/usr/bin/env python3
"""
Apex Secondary Watchdog — Deploy to a SECONDARY machine
Self-contained stdlib-only script. No apex_utils dependency.

Purpose:
  Polls the primary VM health endpoint every 5 minutes (via cron).
  If the primary is unreachable for >15 minutes, queries T212 directly
  to audit open positions and stop-loss protection status, then sends
  a Telegram alert.

Deployment:
  1. Copy this file to a secondary machine (VPS, Raspberry Pi, etc.)
  2. Set the environment variables below (or create /etc/apex-watchdog.env)
  3. Add to crontab: */5 * * * * /usr/bin/python3 /path/to/apex-secondary-watchdog.py
  4. GitHub Actions alternative: see repo wiki for scheduled workflow template

Required configuration (env vars or /etc/apex-watchdog.env):
  PRIMARY_HEALTH_URL   — e.g. http://57.128.167.193:7777/health
  GIST_URL             — raw Gist URL for apex-state.json
                         e.g. https://gist.githubusercontent.com/USER/GIST_ID/raw/apex-state.json
  T212_AUTH            — T212 Basic auth token (same value as T212_AUTH in .env.trading212)
  T212_ENDPOINT        — T212 API base URL (same as T212_ENDPOINT)
  APEX_BOT_TOKEN       — Telegram bot token
  APEX_CHAT_ID         — Telegram chat ID
  HEARTBEAT_FILE       — optional, defaults to /tmp/apex-secondary-heartbeat.json

State is persisted to HEARTBEAT_FILE between cron runs to track consecutive failures.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Configuration ──────────────────────────────────────────────────────────────
_ENV_FILE = '/etc/apex-watchdog.env'

def _load_config():
    cfg = {}
    for path in (_ENV_FILE, os.path.expanduser('~/.apex-watchdog.env')):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, _, v = line.partition('=')
                        cfg[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
    cfg.update(os.environ)
    return cfg

CFG = _load_config()

PRIMARY_HEALTH_URL       = CFG.get('PRIMARY_HEALTH_URL', '')
GIST_URL                 = CFG.get('GIST_URL', '')
T212_AUTH                = CFG.get('T212_AUTH', '')
T212_ENDPOINT            = CFG.get('T212_ENDPOINT', 'https://live.trading212.com/api/v0')
APEX_BOT_TOKEN           = CFG.get('APEX_BOT_TOKEN', '')
APEX_CHAT_ID             = CFG.get('APEX_CHAT_ID', '')
HEARTBEAT_FILE           = CFG.get('HEARTBEAT_FILE', '/tmp/apex-secondary-heartbeat.json')

FAILURE_THRESHOLD_MINUTES = 15
CHECK_TIMEOUT_SECONDS     = 8
T212_TIMEOUT_SECONDS      = 15


# ── Heartbeat persistence ──────────────────────────────────────────────────────

def _load_heartbeat():
    try:
        with open(HEARTBEAT_FILE) as f:
            return json.load(f)
    except Exception:
        return {'consecutive_failures': 0, 'first_failure': None, 'last_alert': None}

def _save_heartbeat(hb):
    try:
        with open(HEARTBEAT_FILE, 'w') as f:
            json.dump(hb, f, indent=2)
    except Exception as e:
        print(f"WARNING: Could not save heartbeat: {e}")


# ── Primary VM health check ────────────────────────────────────────────────────

def check_primary_alive():
    if not PRIMARY_HEALTH_URL:
        print("ERROR: PRIMARY_HEALTH_URL not configured")
        return False
    try:
        req = urllib.request.Request(
            PRIMARY_HEALTH_URL,
            headers={'User-Agent': 'apex-secondary-watchdog/1.0'}
        )
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT_SECONDS) as r:
            return r.status == 200
    except Exception:
        return False


# ── T212 API ───────────────────────────────────────────────────────────────────

def t212_get(path):
    """Minimal stdlib T212 request — no apex_utils dependency."""
    if not T212_AUTH or not T212_ENDPOINT:
        return None
    try:
        req = urllib.request.Request(
            T212_ENDPOINT.rstrip('/') + path,
            headers={
                'Authorization': f'Basic {T212_AUTH}',
                'User-Agent': 'apex-secondary-watchdog/1.0',
            }
        )
        with urllib.request.urlopen(req, timeout=T212_TIMEOUT_SECONDS) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"T212 HTTP {e.code} on {path}: {e.reason}")
        return None
    except Exception as e:
        print(f"T212 request failed ({path}): {e}")
        return None


# ── Gist state fetch ───────────────────────────────────────────────────────────

def fetch_gist_state():
    """Fetch last-known positions from Gist export. Returns dict or {}."""
    if not GIST_URL:
        return {}
    try:
        req = urllib.request.Request(
            GIST_URL,
            headers={'User-Agent': 'apex-secondary-watchdog/1.0',
                     'Cache-Control': 'no-cache'}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f"Gist fetch failed: {e}")
        return {}


# ── Stop protection check ──────────────────────────────────────────────────────

def has_confirmed_stop(ticker, open_orders):
    """
    Mirrors apex-broker-watchdog.py::check_unprotected_positions() logic.
    Returns (protected: bool, order_id: str|None).
    A position is protected only if T212 has a live STOP order
    with status NEW or WORKING.
    """
    for order in (open_orders or []):
        if (order.get('type') == 'STOP'
                and order.get('ticker') == ticker
                and order.get('status') in ('NEW', 'WORKING')):
            return True, str(order.get('id', '?'))
    return False, None


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text):
    if not APEX_BOT_TOKEN or not APEX_CHAT_ID:
        print("Telegram not configured — printing alert to stdout instead")
        print(text)
        return
    try:
        payload = json.dumps({
            'chat_id':    APEX_CHAT_ID,
            'text':       text,
            'parse_mode': 'HTML',
        }).encode('utf-8')
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{APEX_BOT_TOKEN}/sendMessage',
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        print(f"Telegram send failed: {e}")
        print(f"Alert text was:\n{text}")


# ── Stop audit ─────────────────────────────────────────────────────────────────

def run_stop_audit(minutes_down):
    print(f"Primary unreachable for {round(minutes_down)}min — running T212 stop audit")

    portfolio   = t212_get('/equity/portfolio')
    open_orders = t212_get('/equity/orders')
    gist_state  = fetch_gist_state()

    if portfolio is None:
        send_telegram(
            f"<b>APEX PRIMARY UNREACHABLE — {round(minutes_down)}min</b>\n\n"
            f"Could not query T212 portfolio (API unavailable).\n"
            f"Manual check required."
        )
        return

    if open_orders is None:
        open_orders = []

    last_known  = gist_state.get('positions', []) if isinstance(gist_state, dict) else []
    gist_ts     = gist_state.get('exported', 'unknown') if isinstance(gist_state, dict) else 'unknown'
    n_positions = len(portfolio)

    lines = [
        f"<b>APEX PRIMARY UNREACHABLE — {round(minutes_down)}min</b>",
        f"T212 stop audit — {n_positions} open position(s)",
        f"Last state export: {gist_ts}",
        "",
    ]

    unprotected_count = 0
    for pos in portfolio:
        ticker  = pos.get('ticker', '?')
        qty     = pos.get('quantity', 0)
        current = float(pos.get('currentPrice', 0))
        ppl     = float(pos.get('ppl', 0))

        protected, order_id = has_confirmed_stop(ticker, open_orders)

        # Find entry price from last-known Gist state
        known = next((p for p in last_known
                      if p.get('t212_ticker') == ticker
                      or p.get('ticker') == ticker), {})
        entry = float(known.get('entry', current) or current)
        stop_price = known.get('stop', '?')
        pnl_pct = round((current - entry) / entry * 100, 1) if entry else 0

        pnl_sign = '+' if ppl >= 0 else ''
        stop_str = f"@ {stop_price}" if stop_price != '?' else 'unknown level'

        if protected:
            stop_icon = "STOP OK"
            line = (f"{ticker}: qty={qty} | {stop_icon} ({stop_str}) "
                    f"| P&amp;L: {pnl_sign}{round(ppl, 2)} ({pnl_pct}%)")
        else:
            stop_icon = "NO STOP"
            line = (f"<b>{ticker}: qty={qty} | {stop_icon} | "
                    f"P&amp;L: {pnl_sign}{round(ppl, 2)} ({pnl_pct}%)</b>")
            unprotected_count += 1

        lines.append(line)

    if unprotected_count > 0:
        lines.append("")
        lines.append(f"<b>WARNING: {unprotected_count} position(s) have no confirmed stop.</b>")
        lines.append("Check T212 immediately and place stop orders manually if needed.")
    else:
        lines.append("")
        lines.append(f"All {n_positions} position(s) have confirmed stops at T212.")
        lines.append("No immediate action needed — positions are protected.")

    send_telegram('\n'.join(lines))


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    now = datetime.now(timezone.utc)
    hb  = _load_heartbeat()

    alive = check_primary_alive()

    if alive:
        if hb['consecutive_failures'] > 0:
            print(f"Primary recovered after {hb['consecutive_failures']} failed checks")
        hb = {'consecutive_failures': 0, 'first_failure': None, 'last_alert': None}
        _save_heartbeat(hb)
        return

    # Primary is down
    if hb['consecutive_failures'] == 0:
        hb['first_failure'] = now.isoformat()

    hb['consecutive_failures'] += 1
    _save_heartbeat(hb)

    first = datetime.fromisoformat(hb['first_failure']).replace(tzinfo=timezone.utc)
    minutes_down = (now - first).total_seconds() / 60

    print(f"Primary unreachable — failure #{hb['consecutive_failures']}, "
          f"{round(minutes_down)}min since first failure")

    if minutes_down < FAILURE_THRESHOLD_MINUTES:
        return  # Not yet at threshold — wait

    # Throttle repeated alerts: only re-alert every 30 minutes
    last_alert = hb.get('last_alert')
    if last_alert:
        try:
            last_dt = datetime.fromisoformat(last_alert).replace(tzinfo=timezone.utc)
            if (now - last_dt).total_seconds() < 1800:
                print("Alert throttled — already alerted within last 30 minutes")
                return
        except Exception:
            pass

    hb['last_alert'] = now.isoformat()
    _save_heartbeat(hb)

    run_stop_audit(minutes_down)


if __name__ == '__main__':
    if not PRIMARY_HEALTH_URL:
        print("ERROR: PRIMARY_HEALTH_URL not set. "
              "Create /etc/apex-watchdog.env with required variables.")
        sys.exit(1)
    run()
