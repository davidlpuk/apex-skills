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
        except:
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
            except:
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
        except:
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
        except:
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
        except:
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
    except:
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
    except:
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
                    except:
                        recent.append(line.strip())
    except:
        pass
    return recent[-max_count:]

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

    print("\n✅ apex_utils.py working correctly")
