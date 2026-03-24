#!/usr/bin/env python3
"""
Apex Utilities
Shared utility functions used across all Apex scripts.
"""
import json
import os
import tempfile
import logging
from datetime import datetime, timezone

# ============================================================
# ATOMIC FILE WRITES
# ============================================================
def atomic_write(filepath, data):
    """
    Write JSON data atomically — write to temp file then rename.
    Prevents corrupt files from interrupted writes.
    Rename is atomic on Linux — either the old file or new file
    exists, never a partial write.
    """
    dirpath = os.path.dirname(filepath)
    if not dirpath:
        dirpath = '.'

    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=dirpath,
            delete=False,
            suffix='.tmp',
            encoding='utf-8'
        ) as f:
            json.dump(data, f, indent=2, default=str)
            tmp_path = f.name

        # Atomic rename — replaces target file in one operation
        os.replace(tmp_path, filepath)
        return True

    except Exception as e:
        # Clean up temp file if rename failed
        try:
            if 'tmp_path' in locals():
                os.unlink(tmp_path)
        except Exception:
            pass
        log_error(f"atomic_write failed for {filepath}: {e}")
        return False

def safe_read(filepath, default=None):
    """
    Read JSON file safely with validation.
    Returns default if file missing, corrupt, or too old.
    """
    if not os.path.exists(filepath):
        return default if default is not None else {}

    try:
        with open(filepath, encoding='utf-8') as f:
            data = json.load(f)
        return data
    except json.JSONDecodeError as e:
        log_error(f"Corrupt JSON in {filepath}: {e}")
        # Attempt to recover backup
        backup = filepath + '.bak'
        if os.path.exists(backup):
            try:
                with open(backup, encoding='utf-8') as f:
                    data = json.load(f)
                log_warning(f"Recovered {filepath} from backup")
                return data
            except Exception:
                pass
        return default if default is not None else {}
    except Exception as e:
        log_error(f"Failed to read {filepath}: {e}")
        return default if default is not None else {}

def safe_read_validated(filepath, max_age_hours=25, default=None):
    """
    Read JSON file and validate freshness.
    Warns if data is stale but returns it anyway.
    """
    data = safe_read(filepath, default)
    if not data:
        return data

    timestamp = data.get('timestamp', '')
    if timestamp:
        try:
            # Handle both ISO format and human-readable
            if 'UTC' in timestamp:
                ts = datetime.strptime(timestamp, '%Y-%m-%d %H:%M UTC')
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = datetime.fromisoformat(timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age_hours > max_age_hours:
                log_warning(f"{os.path.basename(filepath)} is {age_hours:.1f}h old (max {max_age_hours}h)")
        except Exception:
            pass

    return data

def atomic_write_with_backup(filepath, data):
    """
    Atomic write that also keeps a .bak of the previous version.
    Allows recovery if new data is somehow invalid.
    """
    # Back up existing file first
    if os.path.exists(filepath):
        try:
            backup = filepath + '.bak'
            import shutil
            shutil.copy2(filepath, backup)
        except Exception:
            pass

    return atomic_write(filepath, data)

# ============================================================
# ERROR LOGGING
# ============================================================
LOG_DIR  = '/home/ubuntu/.picoclaw/logs'
LOG_FILE = f'{LOG_DIR}/apex-errors.log'

def _setup_logger():
    logger = logging.getLogger('apex')
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        # File handler
        try:
            fh = logging.FileHandler(LOG_FILE)
            fh.setLevel(logging.DEBUG)
            fmt = logging.Formatter(
                '%(asctime)s | %(levelname)s | %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception:
            pass
        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.WARNING)
        logger.addHandler(ch)
    return logger

def log_error(message, exc=None):
    logger = _setup_logger()
    if exc:
        logger.error(f"{message} | Exception: {exc}")
    else:
        logger.error(message)

def log_warning(message):
    _setup_logger().warning(message)

def log_info(message):
    _setup_logger().info(message)

def log_trade(action, instrument, details):
    """Log trade-specific events to separate trade log."""
    trade_log = f'{LOG_DIR}/apex-trade-events.log'
    try:
        with open(trade_log, 'a') as f:
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            f.write(f"{ts} | {action} | {instrument} | {details}\n")
    except Exception:
        pass

# ============================================================
# CONVENIENCE WRAPPERS
# ============================================================
def load(filename, default=None):
    """Load a file from the logs directory."""
    filepath = f'{LOG_DIR}/{filename}'
    return safe_read(filepath, default)

def save(filename, data, backup=False):
    """Save a file to the logs directory atomically."""
    filepath = f'{LOG_DIR}/{filename}'
    if backup:
        return atomic_write_with_backup(filepath, data)
    return atomic_write(filepath, data)

def rotate_error_log(max_lines=1000):
    """Keep error log from growing indefinitely — keep last 1000 lines."""
    try:
        if not os.path.exists(LOG_FILE):
            return
        with open(LOG_FILE) as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(LOG_FILE, 'w') as f:
                f.writelines(lines[-max_lines:])
    except Exception:
        pass

def get_recent_errors(hours=24, max_count=20):
    """Get recent errors for health check reporting."""
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    try:
        with open(LOG_FILE) as f:
            for line in f:
                if 'ERROR' in line or 'WARNING' in line:
                    try:
                        ts_str = line.split(' | ')[0]
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                        ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            recent.append(line.strip())
                    except Exception:
                        recent.append(line.strip())
    except Exception:
        pass
    return recent[-max_count:]

# ============================================================
# ENVIRONMENT & CONFIGURATION
# ============================================================
_ENV_CACHE = {}

def _load_env(env_file='/home/ubuntu/.picoclaw/.env.trading212'):
    """Parse .env file into dict. Cached after first call."""
    if _ENV_CACHE:
        return _ENV_CACHE
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    _ENV_CACHE[k.strip()] = v.strip()
    except Exception as e:
        log_error(f"_load_env failed: {e}")
    return _ENV_CACHE

def get_t212_endpoint():
    """Return T212 API endpoint from .env.trading212."""
    return _load_env().get('T212_ENDPOINT', '')

# ============================================================
# TELEGRAM MESSAGING
# ============================================================
def send_telegram(message):
    """
    Send a Telegram message using APEX_BOT_TOKEN and APEX_CHAT_ID
    from .env.trading212. Uses stdlib urllib — no subprocess or curl.
    Silent failure — never crashes the caller.
    """
    try:
        import urllib.request
        import urllib.parse

        env = _load_env()
        token   = env.get('APEX_BOT_TOKEN', '')
        chat_id = env.get('APEX_CHAT_ID', '')

        if not token or not chat_id:
            log_error("send_telegram: APEX_BOT_TOKEN or APEX_CHAT_ID not set in .env.trading212")
            return False

        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': chat_id,
            'text': message
        }).encode('utf-8')

        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200

    except Exception as e:
        log_error(f"send_telegram failed: {e}")
        return False

# ============================================================
# PORTFOLIO VALUE
# ============================================================
_PORTFOLIO_CACHE_FILE = f'{LOG_DIR}/apex-portfolio-cache.json'

def get_free_cash(cache_max_age=300):
    """
    Return uninvested cash from the portfolio cache (same TTL as get_portfolio_value).
    Forces a fresh API call if cache is stale.
    Callers should use: get_free_cash() or 0
    """
    cached = safe_read(_PORTFOLIO_CACHE_FILE, {})
    if cached and cached.get('timestamp'):
        try:
            ts = datetime.fromisoformat(cached['timestamp'])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < cache_max_age:
                return float(cached.get('free', 0))
        except Exception:
            pass
    # Cache stale — trigger a portfolio refresh then re-read
    get_portfolio_value(cache_max_age=0)
    cached = safe_read(_PORTFOLIO_CACHE_FILE, {})
    return float(cached.get('free', 0))

def get_portfolio_value(cache_max_age=300):
    """
    Get portfolio value from T212 API with file-based caching.
    Returns float or None on complete failure.
    Callers should use: get_portfolio_value() or 5000
    """
    now = datetime.now(timezone.utc)

    # Check file cache first
    cached = safe_read(_PORTFOLIO_CACHE_FILE, {})
    if cached and cached.get('timestamp'):
        try:
            ts = datetime.fromisoformat(cached['timestamp'])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds()
            if age < cache_max_age:
                return cached.get('value')
        except Exception:
            pass

    # Call T212 API via centralised rate-limited caller
    try:
        data = t212_request('/equity/account/cash', timeout=10)
        if data is None:
            log_error("get_portfolio_value: t212_request returned None")
            return cached.get('value') if cached else None

        free     = float(data.get('free', 0))
        invested = float(data.get('invested', 0))
        total    = round(float(data.get('total', free + invested)), 2)

        # Update cache
        atomic_write(_PORTFOLIO_CACHE_FILE, {
            'value': total,
            'free': free,
            'invested': invested,
            'timestamp': now.isoformat()
        })
        return total

    except Exception as e:
        log_error(f"get_portfolio_value API failed: {e}")
        return cached.get('value') if cached else None

# ============================================================
# FILE LOCKING
# ============================================================
import fcntl
from contextlib import contextmanager
import time

# ============================================================
# T212 RATE LIMITER
# Enforces a minimum gap between T212 API calls across ALL
# processes via a shared file lock. Prevents 429 bursts when
# cron jobs fire simultaneously.
# ============================================================
_T212_RATE_FILE    = f'{LOG_DIR}/apex-t212-last-call'
_T212_MIN_INTERVAL = 0.6   # 600ms minimum between calls

def _t212_rate_limit():
    """
    Acquire the T212 call slot, sleeping if needed to honour the
    minimum inter-call interval. Works across separate processes.
    """
    try:
        with file_lock(_T212_RATE_FILE, timeout=15):
            now = time.time()
            try:
                with open(_T212_RATE_FILE) as f:
                    last_call = float(f.read().strip())
                elapsed = now - last_call
                if elapsed < _T212_MIN_INTERVAL:
                    time.sleep(_T212_MIN_INTERVAL - elapsed)
            except (FileNotFoundError, ValueError, OSError):
                pass  # First call ever — no wait needed
            # Stamp this call
            with open(_T212_RATE_FILE, 'w') as f:
                f.write(str(time.time()))
    except Exception as e:
        log_warning(f"_t212_rate_limit: {e}")

def t212_request(path, method='GET', payload=None, timeout=15, retries=3):
    """
    Centralised T212 API caller.
    - Enforces 600ms rate limit across all processes
    - Auto-retries on TooManyRequests (2s / 5s / 10s backoff)
    - Returns parsed JSON or None on failure

    Usage:
        data = t212_request('/equity/portfolio')
        cash = t212_request('/equity/account/cash')
        result = t212_request('/equity/orders/stop', method='POST',
                              payload={'ticker': ..., ...})
    """
    import urllib.request as _ur
    import urllib.error  as _ue

    env      = _load_env()
    auth     = env.get('T212_AUTH', '')
    endpoint = env.get('T212_ENDPOINT', '').rstrip('/')

    if not auth or not endpoint:
        log_error("t212_request: T212_AUTH or T212_ENDPOINT not configured")
        return None

    url     = f"{endpoint}{path}"
    delays  = [2, 5, 10]

    for attempt in range(retries + 1):
        _t212_rate_limit()
        try:
            headers = {
                'Authorization': f'Basic {auth}',
                'User-Agent': 'Mozilla/5.0',
            }
            body = None
            if payload is not None:
                body = json.dumps(payload).encode('utf-8')
                headers['Content-Type'] = 'application/json'

            req = _ur.Request(url, data=body, headers=headers, method=method)
            with _ur.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))

            # T212 sometimes returns 200 with a TooManyRequests body
            if (isinstance(result, dict) and
                    result.get('code') == 'BusinessException' and
                    isinstance(result.get('context'), dict) and
                    result['context'].get('type') == 'TooManyRequests'):
                if attempt < len(delays):
                    wait = delays[attempt]
                    log_warning(f"T212 TooManyRequests on {path} — retry {attempt+1} in {wait}s")
                    time.sleep(wait)
                    continue
                log_error(f"T212 TooManyRequests on {path} after {attempt+1} attempts")
                return None

            return result

        except _ue.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < len(delays):
                wait = delays[attempt]
                log_warning(f"T212 HTTP {e.code} on {path} — retry {attempt+1} in {wait}s")
                time.sleep(wait)
                continue
            log_error(f"t212_request HTTPError {e.code} on {path}: {e}")
            return None
        except Exception as e:
            if attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            log_error(f"t212_request failed on {path}: {e}")
            return None

    return None

@contextmanager
def file_lock(filepath, timeout=5):
    """
    Acquire exclusive file lock using fcntl.flock().
    Uses a .lock sidecar file to avoid interfering with readers.
    """
    lock_path = filepath + '.lock'
    lock_fd = None
    try:
        lock_fd = open(lock_path, 'w')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (IOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Could not acquire lock on {filepath} within {timeout}s")
                time.sleep(0.05)
        yield lock_fd
    finally:
        if lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass

def locked_read_modify_write(filepath, modifier_fn, default=None):
    """
    Lock file, read JSON, apply modifier_fn, write atomically.
    modifier_fn receives the data and should return modified data.
    """
    with file_lock(filepath):
        data = safe_read(filepath, default)
        modified = modifier_fn(data)
        return atomic_write(filepath, modified)

# ============================================================
# TICKER MAP UTILITIES
# ============================================================
_TICKER_MAP_CACHE = None
_TICKER_REVERSE_CACHE = None  # t212 ticker → yahoo ticker

TICKER_MAP_PATH = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'

def _load_ticker_map():
    """Load and cache the canonical ticker map."""
    global _TICKER_MAP_CACHE, _TICKER_REVERSE_CACHE
    if _TICKER_MAP_CACHE is not None:
        return _TICKER_MAP_CACHE, _TICKER_REVERSE_CACHE

    _TICKER_MAP_CACHE = safe_read(TICKER_MAP_PATH, {})
    _TICKER_REVERSE_CACHE = {}

    for yahoo_key, entry in _TICKER_MAP_CACHE.items():
        if not isinstance(entry, dict):
            continue
        t212 = entry.get('t212', '')
        if t212:
            _TICKER_REVERSE_CACHE[t212] = yahoo_key
            _TICKER_REVERSE_CACHE[t212.upper()] = yahoo_key

    return _TICKER_MAP_CACHE, _TICKER_REVERSE_CACHE

def get_yahoo_ticker(name_or_t212):
    """
    Look up Yahoo Finance ticker from a name or T212 ticker.
    Uses the canonical apex-ticker-map.json with derivation fallback.
    """
    tmap, reverse = _load_ticker_map()

    # Direct match in ticker map (name is a yahoo key like "AAPL", "HSBA")
    if name_or_t212 in tmap and isinstance(tmap[name_or_t212], dict):
        entry = tmap[name_or_t212]
        currency = entry.get('currency', 'USD')
        if currency in ('GBX', 'GBP'):
            return f"{name_or_t212}.L"
        return name_or_t212

    # Reverse lookup: t212 ticker → yahoo key
    if name_or_t212 in reverse:
        yahoo_key = reverse[name_or_t212]
        entry = tmap.get(yahoo_key, {})
        currency = entry.get('currency', 'USD')
        if currency in ('GBX', 'GBP'):
            return f"{yahoo_key}.L"
        return yahoo_key

    # Derivation fallback: strip T212 suffixes
    clean = name_or_t212
    for suffix in ('_US_EQ', 'l_EQ', 's_EQ', 'd_EQ', 'm_EQ', 'p_EQ', '_EQ'):
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break

    # Check if the cleaned version is in the ticker map
    if clean in tmap and isinstance(tmap[clean], dict):
        entry = tmap[clean]
        currency = entry.get('currency', 'USD')
        if currency in ('GBX', 'GBP'):
            return f"{clean}.L"
        return clean

    return clean

def get_t212_ticker(name):
    """Look up T212 ticker from a name/yahoo key."""
    tmap, _ = _load_ticker_map()
    # Strip .L suffix for lookup
    lookup = name.replace('.L', '')
    entry = tmap.get(lookup, {})
    return entry.get('t212', '')


if __name__ == '__main__':
    # Test atomic write
    test_data = {'test': True, 'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    result = atomic_write('/tmp/apex_test.json', test_data)
    print(f"Atomic write test: {'✅ OK' if result else '❌ FAILED'}")

    # Test safe read
    data = safe_read('/tmp/apex_test.json')
    print(f"Safe read test: {'✅ OK' if data.get('test') else '❌ FAILED'}")

    # Test backup write
    result = atomic_write_with_backup('/tmp/apex_test.json', {'updated': True})
    print(f"Backup write test: {'✅ OK' if result else '❌ FAILED'}")
    backup_exists = os.path.exists('/tmp/apex_test.json.bak')
    print(f"Backup created: {'✅ OK' if backup_exists else '❌ FAILED'}")


# ============================================================
# ALPHA VANTAGE UTILITY
# Free-tier API: 25 calls/day. Use sparingly.
# ============================================================
def _get_alpha_vantage_key():
    """Read Alpha Vantage API key from env file or environment."""
    try:
        with open(f'{BASE_DIR}/.env.trading212') as f:
            for line in f:
                line = line.strip()
                if line.startswith('ALPHA_VANTAGE_KEY=') or line.startswith('AV_KEY='):
                    return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return os.environ.get('ALPHA_VANTAGE_KEY', os.environ.get('AV_KEY', ''))


def alpha_vantage_request(symbol, function='GLOBAL_QUOTE'):
    """
    Make an Alpha Vantage API request (free tier: 25 calls/day).
    Returns parsed JSON dict or None on failure.

    Common functions:
      GLOBAL_QUOTE        — latest price, volume, change
      TIME_SERIES_DAILY   — daily OHLCV (add outputsize=full for 20 years)
      OVERVIEW            — company fundamentals
    """
    import urllib.request
    av_key = _get_alpha_vantage_key()
    if not av_key:
        log_warning("Alpha Vantage: no API key configured (set ALPHA_VANTAGE_KEY in .env.trading212)")
        return None
    try:
        url = (f"https://www.alphavantage.co/query"
               f"?function={function}&symbol={symbol}&apikey={av_key}")
        req = urllib.request.Request(url, headers={'User-Agent': 'ApexBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            # AV returns {"Note": "..."} or {"Information": "..."} on rate-limit
            if 'Note' in data or 'Information' in data:
                log_warning(f"Alpha Vantage rate limit hit for {symbol}")
                return None
            return data
    except Exception as e:
        log_error(f"Alpha Vantage request failed ({function}, {symbol}): {e}")
        return None

    print("\n✅ apex_utils.py working correctly")
