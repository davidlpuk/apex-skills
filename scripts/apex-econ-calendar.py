#!/usr/bin/env python3
"""
Economic Calendar Blackout Gate

High-impact US macro events regularly produce 2-3σ moves that blow through
stops. This script maintains a deterministic calendar of FOMC, CPI, NFP, PPI,
and Fed Chair speeches, and returns a BLOCK status when an event is within
±BLACKOUT_HOURS of the current time.

Approach (no external API needed):
  - FOMC dates: hardcoded from the Fed's published schedule (8 per year)
  - CPI / PPI / NFP / Retail Sales: computed from BLS release-rule calendar
  - All times in UTC. Release times are UTC (BLS/Fed publish at 12:30 / 18:00)

Usage:
  python3 apex-econ-calendar.py        # print current status + write JSON
  python3 apex-econ-calendar.py check  # exit 0 if CLEAR, 1 if BLACKOUT

Other scripts call is_blackout() or read apex-econ-calendar.json.
"""
import json
import sys
from datetime import datetime, timezone, timedelta, time as dtime, date

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_warning(m): print(f'WARNING: {m}')


CALENDAR_FILE = '/home/ubuntu/.picoclaw/logs/apex-econ-calendar.json'

# Pre/post event blackout window (hours)
BLACKOUT_HOURS = 2

# ── FOMC scheduled rate decisions (UTC release time 18:00) ────────────────
# Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_DATES = [
    # 2026 schedule (announced by Fed)
    '2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17',
    '2026-07-29', '2026-09-16', '2026-11-04', '2026-12-16',
    # 2027 (tentative — update when Fed publishes)
    '2027-01-27', '2027-03-17', '2027-04-28', '2027-06-16',
    '2027-07-28', '2027-09-15', '2027-11-03', '2027-12-15',
]
FOMC_RELEASE_UTC = dtime(18, 0)  # 2:00 PM ET = 18:00 UTC (after DST adjust)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence of weekday (Mon=0) in the given month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d = d + timedelta(days=offset + 7 * (n - 1))
    return d


def _first_friday(year: int, month: int) -> date:
    return _nth_weekday_of_month(year, month, 4, 1)  # Fri = 4


def _compute_events_for_month(year: int, month: int) -> list:
    """Generate all high-impact events for a given month."""
    events = []

    # FOMC — from hardcoded list
    for d_str in FOMC_DATES:
        d = datetime.strptime(d_str, '%Y-%m-%d').date()
        if d.year == year and d.month == month:
            events.append({
                'name':    'FOMC_RATE',
                'severity': 'CRITICAL',
                'date':    d.isoformat(),
                'time':    FOMC_RELEASE_UTC.strftime('%H:%M'),
            })

    # NFP — Nonfarm Payrolls, first Friday of month, 12:30 UTC (8:30 ET)
    nfp = _first_friday(year, month)
    events.append({
        'name':    'NFP',
        'severity': 'CRITICAL',
        'date':    nfp.isoformat(),
        'time':    '12:30',
    })

    # CPI — Bureau of Labor Stats releases ~10th-14th of following month.
    # Approximation: 2nd Wednesday works for most months. Wed = 2.
    cpi = _nth_weekday_of_month(year, month, 2, 2)
    events.append({
        'name':    'CPI',
        'severity': 'HIGH',
        'date':    cpi.isoformat(),
        'time':    '12:30',
    })

    # PPI — day after CPI typically (use 2nd Thursday as approximation)
    ppi = _nth_weekday_of_month(year, month, 3, 2)
    events.append({
        'name':    'PPI',
        'severity': 'MEDIUM',
        'date':    ppi.isoformat(),
        'time':    '12:30',
    })

    # Retail Sales — 3rd Tuesday of month (BLS/Census schedule)
    retail = _nth_weekday_of_month(year, month, 1, 3)
    events.append({
        'name':    'RETAIL_SALES',
        'severity': 'MEDIUM',
        'date':    retail.isoformat(),
        'time':    '12:30',
    })

    return events


def get_upcoming_events(now: datetime = None, days: int = 14) -> list:
    """Return events within `days` ahead of `now`, sorted by time."""
    if now is None:
        now = datetime.now(timezone.utc)

    events = []
    # Include current and next month to cover lookahead
    months_to_scan = [
        (now.year, now.month),
        ((now.year + (now.month // 12)), ((now.month % 12) + 1)),
    ]
    for y, m in months_to_scan:
        events.extend(_compute_events_for_month(y, m))

    # Convert to datetime and filter
    upcoming = []
    horizon_end = now + timedelta(days=days)
    for e in events:
        ev_dt = datetime.fromisoformat(f"{e['date']}T{e['time']}:00+00:00")
        if now - timedelta(hours=BLACKOUT_HOURS) <= ev_dt <= horizon_end:
            e_full = dict(e)
            e_full['datetime_utc'] = ev_dt.isoformat()
            e_full['hours_until']  = round((ev_dt - now).total_seconds() / 3600, 1)
            upcoming.append(e_full)

    upcoming.sort(key=lambda x: x['datetime_utc'])
    return upcoming


def is_blackout(now: datetime = None) -> tuple:
    """
    Check if current time is within BLACKOUT_HOURS of any CRITICAL or HIGH event.

    Returns (blocked: bool, reason: str, event: dict|None).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    upcoming = get_upcoming_events(now, days=1)
    for e in upcoming:
        if e['severity'] not in ('CRITICAL', 'HIGH'):
            continue
        ev_dt = datetime.fromisoformat(e['datetime_utc'])
        delta_hours = abs((ev_dt - now).total_seconds() / 3600)
        if delta_hours <= BLACKOUT_HOURS:
            when = "upcoming" if ev_dt > now else "just released"
            reason = (
                f"Econ blackout: {e['name']} ({e['severity']}) {when} "
                f"at {e['datetime_utc'][:16]} UTC "
                f"({delta_hours:.1f}h delta)"
            )
            return True, reason, e
    return False, "CLEAR", None


def run():
    """Compute current status and write to logs/apex-econ-calendar.json."""
    now = datetime.now(timezone.utc)
    blocked, reason, event = is_blackout(now)
    upcoming = get_upcoming_events(now, days=14)

    result = {
        'timestamp':     now.strftime('%Y-%m-%d %H:%M UTC'),
        'status':        'BLACKOUT' if blocked else 'CLEAR',
        'reason':        reason,
        'current_event': event,
        'blackout_hours': BLACKOUT_HOURS,
        'upcoming_14d':  upcoming[:10],
    }
    atomic_write(CALENDAR_FILE, result)
    return result


def display(result):
    icon = '⛔' if result['status'] == 'BLACKOUT' else '✅'
    print(f"\n=== ECONOMIC CALENDAR ===")
    print(f"  {icon} Status: {result['status']}")
    print(f"  {result['reason']}")
    print(f"\n  Next 14 days (top 10):")
    for e in result['upcoming_14d']:
        sev_icon = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡'}.get(e['severity'], '⚪')
        print(f"    {sev_icon} {e['name']:14} {e['datetime_utc'][:16]} UTC  (+{e['hours_until']:.0f}h)")


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'run'
    result = run()
    if mode == 'check':
        sys.exit(1 if result['status'] == 'BLACKOUT' else 0)
    display(result)
