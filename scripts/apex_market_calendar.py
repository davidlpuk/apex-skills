#!/usr/bin/env python3
"""
Market Holiday Calendar
Prevents Apex running on days when markets are closed.
Covers UK (LSE) and US (NYSE/NASDAQ) holidays.

Used by:
- Morning scan — skip if market closed
- Trade queue — delay execution to next open day
- Cron gate — prevent pointless scans on holidays
"""
import json
import sys
from datetime import datetime, date, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import atomic_write, safe_read, log_error, log_warning
except ImportError:
    def atomic_write(p, d):
        with open(p, 'w') as f: json.dump(d, f, indent=2)
        return True
    def log_error(m): print(f'ERROR: {m}')
    def log_warning(m): print(f'WARNING: {m}')

CALENDAR_FILE = '/home/ubuntu/.picoclaw/logs/apex-market-calendar.json'

# ============================================================
# HOLIDAY DEFINITIONS
# Updated annually — covers 2025 and 2026
# ============================================================

# US Market Holidays (NYSE/NASDAQ)
US_HOLIDAYS = {
    # 2025
    '2025-01-01': "New Year's Day",
    '2025-01-20': "Martin Luther King Jr. Day",
    '2025-02-17': "Presidents' Day",
    '2025-04-18': "Good Friday",
    '2025-05-26': "Memorial Day",
    '2025-06-19': "Juneteenth",
    '2025-07-04': "Independence Day",
    '2025-09-01': "Labor Day",
    '2025-11-27': "Thanksgiving Day",
    '2025-12-25': "Christmas Day",
    # 2026
    '2026-01-01': "New Year's Day",
    '2026-01-19': "Martin Luther King Jr. Day",
    '2026-02-16': "Presidents' Day",
    '2026-04-03': "Good Friday",
    '2026-05-25': "Memorial Day",
    '2026-06-19': "Juneteenth",
    '2026-07-03': "Independence Day (observed)",
    '2026-09-07': "Labor Day",
    '2026-11-26': "Thanksgiving Day",
    '2026-12-25': "Christmas Day",
}

# US Early Close Days (1:00 PM EST = 18:00 UTC)
US_EARLY_CLOSE = {
    '2025-07-03': "Independence Day Eve",
    '2025-11-28': "Black Friday",
    '2025-12-24': "Christmas Eve",
    '2026-07-02': "Independence Day Eve",
    '2026-11-27': "Black Friday",
    '2026-12-24': "Christmas Eve",
}

# UK Market Holidays (LSE)
UK_HOLIDAYS = {
    # 2025
    '2025-01-01': "New Year's Day",
    '2025-04-18': "Good Friday",
    '2025-04-21': "Easter Monday",
    '2025-05-05': "Early May Bank Holiday",
    '2025-05-26': "Spring Bank Holiday",
    '2025-08-25': "Summer Bank Holiday",
    '2025-12-25': "Christmas Day",
    '2025-12-26': "Boxing Day",
    # 2026
    '2026-01-01': "New Year's Day",
    '2026-04-03': "Good Friday",
    '2026-04-06': "Easter Monday",
    '2026-05-04': "Early May Bank Holiday",
    '2026-05-25': "Spring Bank Holiday",
    '2026-08-31': "Summer Bank Holiday",
    '2026-12-25': "Christmas Day",
    '2026-12-28': "Boxing Day (observed)",
}

def is_weekend(check_date=None):
    """Check if date is weekend."""
    d = check_date or date.today()
    return d.weekday() >= 5  # Saturday=5, Sunday=6

def is_us_holiday(check_date=None):
    """Check if US markets are closed."""
    d = check_date or date.today()
    date_str = d.strftime('%Y-%m-%d')
    return date_str in US_HOLIDAYS, US_HOLIDAYS.get(date_str, '')

def is_uk_holiday(check_date=None):
    """Check if UK markets are closed."""
    d = check_date or date.today()
    date_str = d.strftime('%Y-%m-%d')
    return date_str in UK_HOLIDAYS, UK_HOLIDAYS.get(date_str, '')

def is_us_early_close(check_date=None):
    """Check if US markets close early today."""
    d = check_date or date.today()
    date_str = d.strftime('%Y-%m-%d')
    return date_str in US_EARLY_CLOSE, US_EARLY_CLOSE.get(date_str, '')

def get_market_status(check_date=None):
    """
    Get full market status for a given date.
    Returns dict with US and UK status.
    """
    d    = check_date or datetime.now(timezone.utc).date()
    now  = datetime.now(timezone.utc)

    weekend = is_weekend(d)
    us_hol, us_hol_name  = is_us_holiday(d)
    uk_hol, uk_hol_name  = is_uk_holiday(d)
    us_early, us_early_name = is_us_early_close(d)

    # US market hours: 14:30-21:00 UTC (09:30-16:00 EST)
    # Early close: 14:30-18:00 UTC
    hour_utc = now.hour * 60 + now.minute
    us_open  = not (weekend or us_hol)
    uk_open  = not (weekend or uk_hol)

    us_currently_open = (
        us_open and
        14*60+30 <= hour_utc <= (18*60 if us_early else 21*60)
        if d == now.date() else us_open
    )
    uk_currently_open = (
        uk_open and
        8*60 <= hour_utc <= 16*60+30
        if d == now.date() else uk_open
    )

    # Overall tradeable status
    if weekend:
        status  = 'CLOSED_WEEKEND'
        message = 'Weekend — markets closed'
    elif us_hol and uk_hol:
        status  = 'CLOSED_HOLIDAY'
        message = f'Both markets closed — US: {us_hol_name}, UK: {uk_hol_name}'
    elif us_hol:
        status  = 'US_CLOSED'
        message = f'US closed ({us_hol_name}) — UK instruments only'
    elif uk_hol:
        status  = 'UK_CLOSED'
        message = f'UK closed ({uk_hol_name}) — US instruments only'
    elif us_early:
        status  = 'US_EARLY_CLOSE'
        message = f'US early close at 18:00 UTC ({us_early_name})'
    else:
        status  = 'OPEN'
        message = 'Both markets open'

    return {
        'date':               d.strftime('%Y-%m-%d'),
        'status':             status,
        'message':            message,
        'us_open':            us_open,
        'uk_open':            uk_open,
        'us_currently_open':  us_currently_open,
        'uk_currently_open':  uk_currently_open,
        'us_holiday':         us_hol_name if us_hol else None,
        'uk_holiday':         uk_hol_name if uk_hol else None,
        'us_early_close':     us_early_name if us_early else None,
        'can_trade_us':       us_open,
        'can_trade_uk':       uk_open,
        'safe_to_scan':       not (weekend or (us_hol and uk_hol)),
    }

def get_next_trading_day(from_date=None):
    """Get next day when both US and UK markets are open."""
    d = from_date or datetime.now(timezone.utc).date()
    d = d + timedelta(days=1)  # Start from tomorrow

    for _ in range(14):  # Max 2 weeks forward
        status = get_market_status(d)
        if status['status'] == 'OPEN':
            return d
        d = d + timedelta(days=1)

    return None

def should_scan_today():
    """
    Quick check — should Apex run the morning scan today?
    Returns (should_scan, reason)
    """
    status = get_market_status()

    if status['status'] in ['CLOSED_WEEKEND', 'CLOSED_HOLIDAY']:
        return False, status['message']

    if not status['safe_to_scan']:
        return False, status['message']

    return True, status['message']

def filter_signals_by_market(signals):
    """
    Filter signals to only include instruments from open markets.
    Called before morning scan executes signals.
    """
    status = get_market_status()

    filtered  = []
    excluded  = []

    for signal in signals:
        ticker   = signal.get('t212_ticker', '')
        is_us    = '_US_EQ' in ticker
        is_uk    = ticker.endswith('l_EQ') or ticker.endswith('_EQ') and not is_us

        if is_us and not status['can_trade_us']:
            excluded.append(f"{signal.get('name','?')} — US market closed ({status['us_holiday']})")
            continue

        if is_uk and not status['can_trade_uk']:
            excluded.append(f"{signal.get('name','?')} — UK market closed ({status['uk_holiday']})")
            continue

        filtered.append(signal)

    return filtered, excluded

def run():
    """Generate market calendar status and save."""
    now    = datetime.now(timezone.utc)
    today  = now.date()
    status = get_market_status(today)

    print(f"\n=== MARKET CALENDAR ===")
    print(f"Date: {today.strftime('%A %d %B %Y')}")
    print(f"Status: {status['status']}")
    print(f"Message: {status['message']}")

    # Next 10 trading days
    print(f"\n  Upcoming schedule:")
    d = today
    shown = 0
    for _ in range(21):
        d = d + timedelta(days=1)
        s = get_market_status(d)
        if s['status'] != 'OPEN':
            icon = "🔴" if 'CLOSED' in s['status'] else "🟡"
            print(f"  {icon} {d.strftime('%a %d %b')}: {s['message']}")
        else:
            if shown < 3:
                print(f"  ✅ {d.strftime('%a %d %b')}: Open")
                shown += 1

    next_trading = get_next_trading_day()
    if next_trading and status['status'] != 'OPEN':
        print(f"\n  Next trading day: {next_trading.strftime('%A %d %B %Y')}")

    # Save
    output = {
        'timestamp':      now.strftime('%Y-%m-%d %H:%M UTC'),
        'today':          status,
        'next_trading':   next_trading.strftime('%Y-%m-%d') if next_trading else None,
    }
    atomic_write(CALENDAR_FILE, output)

    # Warn if today is a holiday
    if status['status'] not in ['OPEN', 'US_EARLY_CLOSE']:
        print(f"\n  ⚠️  Today is not a full trading day")
        print(f"  Morning scan will be skipped or filtered")

    print(f"\n✅ Calendar saved")
    return output

if __name__ == '__main__':
    result = run()
    should, reason = should_scan_today()
    print(f"\n  Should scan today: {'✅ YES' if should else '❌ NO'} — {reason}")
