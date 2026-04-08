#!/usr/bin/env python3
"""
Apex State Database
===================
SQLite-backed state module for apex-positions.json and apex-pending-signal.json.

Migration strategy (dual-write, zero-risk transition):
  Phase 1 (current): All WRITE operations go to BOTH JSON and SQLite.
                     All READ operations still read from JSON (no behaviour change).
                     SQLite becomes a hot-backup and audit trail.
  Phase 2 (future):  Swap reads to SQLite, keep JSON as output for dashboards.

This design means:
  - Zero risk to live trading (reads unchanged)
  - Any SQLite error is logged and swallowed (never blocks execution)
  - Positions and pending signals are recoverable from SQLite if JSON corrupts
  - stop_drift_log provides a persistent audit trail (not available in JSON)

Database: ~/.picoclaw/data/apex-trading.db (SQLite WAL mode, FK enabled)
"""
import json
import sqlite3
import os
from datetime import datetime, timezone

DB_PATH       = '/home/ubuntu/.picoclaw/data/apex-trading.db'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
SIGNAL_FILE    = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    t212_ticker     TEXT    NOT NULL,
    name            TEXT,
    quantity        REAL,
    entry           REAL,
    stop            REAL,
    target1         REAL,
    target2         REAL,
    score           REAL,
    rsi             REAL,
    macd            REAL,
    sector          TEXT,
    atr             REAL,
    signal_type     TEXT,
    currency        TEXT,
    status          TEXT,
    stop_order_id   TEXT,
    entry_order_id  TEXT,
    order_type      TEXT,
    venue           TEXT,
    unprotected     INTEGER DEFAULT 0,
    opened          TEXT,
    opened_iso      TEXT,
    current         REAL,
    ppl             REAL,
    mae_pct         REAL,
    mfe_pct         REAL,
    created_at      TEXT    DEFAULT (datetime('now','utc')),
    updated_at      TEXT    DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS pending_signal (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    name            TEXT,
    signal_type     TEXT,
    entry           REAL,
    stop            REAL,
    target1         REAL,
    target2         REAL,
    score           REAL,
    generated_at    TEXT,
    raw_json        TEXT,
    created_at      TEXT    DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS stop_drift_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    stop_local  REAL,
    stop_t212   REAL,
    delta       REAL,
    detected_at TEXT    DEFAULT (datetime('now','utc'))
);
"""


# ── Connection ───────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """
    Open (or create) the database with WAL mode and FK enabled.
    Applies the schema DDL on first call.
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    db.commit()
    return db


# ── Positions ────────────────────────────────────────────────────────────────

def save_positions(rows: list) -> None:
    """
    Replace the entire positions table with *rows*.

    Called alongside any write to apex-positions.json.  Uses a single
    transaction so the table is never in a partial state.
    """
    try:
        db = get_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        with db:
            db.execute("DELETE FROM positions")
            for row in (rows or []):
                db.execute("""
                    INSERT INTO positions (
                        t212_ticker, name, quantity, entry, stop,
                        target1, target2, score, rsi, macd,
                        sector, atr, signal_type, currency, status,
                        stop_order_id, entry_order_id, order_type, venue,
                        unprotected, opened, opened_iso,
                        current, ppl, mae_pct, mfe_pct, updated_at
                    ) VALUES (
                        :t212_ticker, :name, :quantity, :entry, :stop,
                        :target1, :target2, :score, :rsi, :macd,
                        :sector, :atr, :signal_type, :currency, :status,
                        :stop_order_id, :entry_order_id, :order_type, :venue,
                        :unprotected, :opened, :opened_iso,
                        :current, :ppl, :mae_pct, :mfe_pct, :updated_at
                    )
                """, {
                    't212_ticker':    row.get('t212_ticker', ''),
                    'name':           row.get('name'),
                    'quantity':       row.get('quantity'),
                    'entry':          row.get('entry'),
                    'stop':           row.get('stop'),
                    'target1':        row.get('target1'),
                    'target2':        row.get('target2'),
                    'score':          row.get('score'),
                    'rsi':            row.get('rsi'),
                    'macd':           row.get('macd'),
                    'sector':         row.get('sector'),
                    'atr':            row.get('atr'),
                    'signal_type':    row.get('signal_type'),
                    'currency':       row.get('currency'),
                    'status':         row.get('status'),
                    'stop_order_id':  row.get('stop_order_id'),
                    'entry_order_id': row.get('entry_order_id'),
                    'order_type':     row.get('order_type'),
                    'venue':          row.get('venue'),
                    'unprotected':    1 if row.get('unprotected') else 0,
                    'opened':         row.get('opened'),
                    'opened_iso':     row.get('opened_iso'),
                    'current':        row.get('current'),
                    'ppl':            row.get('ppl'),
                    'mae_pct':        row.get('mae_pct'),
                    'mfe_pct':        row.get('mfe_pct'),
                    'updated_at':     now_iso,
                })
        db.close()
    except Exception as e:
        # Never let a SQLite error block execution — JSON is still the source of truth
        try:
            from apex_utils import log_warning
            log_warning(f"apex-state-db save_positions failed (non-blocking): {e}")
        except Exception:
            print(f"WARNING: apex-state-db save_positions failed: {e}")


def load_positions() -> list:
    """
    Read positions from SQLite (for recovery / audit only — normal reads use JSON).
    Returns a list of dicts with the same schema as apex-positions.json.
    """
    try:
        db = get_db()
        rows = db.execute("SELECT * FROM positions").fetchall()
        db.close()
        result = []
        for row in rows:
            d = dict(row)
            d['unprotected'] = bool(d.get('unprotected'))
            # Remove internal SQLite fields
            d.pop('id', None)
            d.pop('created_at', None)
            d.pop('updated_at', None)
            result.append(d)
        return result
    except Exception as e:
        try:
            from apex_utils import log_warning
            log_warning(f"apex-state-db load_positions failed: {e}")
        except Exception:
            pass
        return []


# ── Pending Signal ───────────────────────────────────────────────────────────

def save_pending_signal(signal: dict) -> None:
    """
    Upsert the pending signal (table holds at most one row with id=1).
    Called alongside any write to apex-pending-signal.json.
    """
    try:
        db = get_db()
        with db:
            db.execute("DELETE FROM pending_signal")
            db.execute("""
                INSERT INTO pending_signal
                    (id, name, signal_type, entry, stop, target1, target2,
                     score, generated_at, raw_json)
                VALUES (1, :name, :signal_type, :entry, :stop, :target1, :target2,
                        :score, :generated_at, :raw_json)
            """, {
                'name':         signal.get('name'),
                'signal_type':  signal.get('signal_type'),
                'entry':        signal.get('entry'),
                'stop':         signal.get('stop'),
                'target1':      signal.get('target1'),
                'target2':      signal.get('target2'),
                'score':        signal.get('score'),
                'generated_at': signal.get('generated_at'),
                'raw_json':     json.dumps(signal),
            })
        db.close()
    except Exception as e:
        try:
            from apex_utils import log_warning
            log_warning(f"apex-state-db save_pending_signal failed (non-blocking): {e}")
        except Exception:
            print(f"WARNING: apex-state-db save_pending_signal failed: {e}")


def load_pending_signal() -> dict | None:
    """
    Read pending signal from SQLite (for recovery / audit only).
    Returns the signal dict or None if no pending signal.
    """
    try:
        db = get_db()
        row = db.execute("SELECT * FROM pending_signal WHERE id = 1").fetchone()
        db.close()
        if row is None:
            return None
        d = dict(row)
        # Restore full signal from raw_json if available
        if d.get('raw_json'):
            try:
                return json.loads(d['raw_json'])
            except Exception:
                pass
        d.pop('id', None)
        d.pop('created_at', None)
        d.pop('raw_json', None)
        return d
    except Exception as e:
        try:
            from apex_utils import log_warning
            log_warning(f"apex-state-db load_pending_signal failed: {e}")
        except Exception:
            pass
        return None


def clear_pending_signal() -> None:
    """
    Remove the pending signal row (called on ABORT or successful execution).
    Mirrors the os.remove(SIGNAL_FILE) call in autopilot / executor.
    """
    try:
        db = get_db()
        with db:
            db.execute("DELETE FROM pending_signal")
        db.close()
    except Exception as e:
        try:
            from apex_utils import log_warning
            log_warning(f"apex-state-db clear_pending_signal failed (non-blocking): {e}")
        except Exception:
            pass


# ── Stop Drift Audit Log ─────────────────────────────────────────────────────

def log_stop_drift(ticker: str, stop_local: float, stop_t212: float | None,
                   delta: float | None) -> None:
    """
    Append a stop drift event to the audit log.
    Called by apex-broker-watchdog when drift is detected.
    """
    try:
        db = get_db()
        with db:
            db.execute("""
                INSERT INTO stop_drift_log (ticker, stop_local, stop_t212, delta)
                VALUES (?, ?, ?, ?)
            """, (ticker, stop_local, stop_t212, delta))
        db.close()
    except Exception as e:
        try:
            from apex_utils import log_warning
            log_warning(f"apex-state-db log_stop_drift failed (non-blocking): {e}")
        except Exception:
            pass


def get_stop_drift_history(ticker: str = None, limit: int = 50) -> list:
    """Return recent stop drift events, optionally filtered by ticker."""
    try:
        db = get_db()
        if ticker:
            rows = db.execute(
                "SELECT * FROM stop_drift_log WHERE ticker = ? ORDER BY id DESC LIMIT ?",
                (ticker, limit)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM stop_drift_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Migration helpers ────────────────────────────────────────────────────────

def migrate_from_json() -> dict:
    """
    One-shot migration: read apex-positions.json and apex-pending-signal.json
    and write both to SQLite.  Safe to run multiple times (idempotent).

    Returns {'positions': n, 'pending_signal': bool}
    """
    result = {'positions': 0, 'pending_signal': False}

    # Positions
    try:
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
        save_positions(positions)
        result['positions'] = len(positions)
        print(f"  Migrated {len(positions)} positions to SQLite")
    except FileNotFoundError:
        print(f"  No positions file found — skipping")
    except Exception as e:
        print(f"  Positions migration failed: {e}")

    # Pending signal
    try:
        with open(SIGNAL_FILE) as f:
            signal = json.load(f)
        save_pending_signal(signal)
        result['pending_signal'] = True
        print(f"  Migrated pending signal: {signal.get('name', '?')}")
    except FileNotFoundError:
        print(f"  No pending signal file — skipping")
    except Exception as e:
        print(f"  Pending signal migration failed: {e}")

    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Apex State Database')
    parser.add_argument('--migrate', action='store_true',
                        help='Migrate existing JSON state files into SQLite')
    parser.add_argument('--status', action='store_true',
                        help='Show current database contents')
    parser.add_argument('--drift-log', action='store_true',
                        help='Show stop drift audit log')
    args = parser.parse_args()

    if args.migrate:
        print("Migrating JSON state to SQLite...")
        result = migrate_from_json()
        print(f"Done: {result}")

    elif args.status:
        positions = load_positions()
        signal    = load_pending_signal()
        print(f"SQLite state at {DB_PATH}")
        print(f"  Positions: {len(positions)}")
        for p in positions:
            print(f"    {p.get('t212_ticker'):20s} {p.get('status'):15s} stop={p.get('stop')}")
        if signal:
            print(f"  Pending signal: {signal.get('name')} (score={signal.get('score')})")
        else:
            print(f"  Pending signal: none")

    elif args.drift_log:
        history = get_stop_drift_history(limit=20)
        print(f"Stop drift audit log (last {len(history)} events):")
        for h in history:
            print(f"  {h.get('detected_at')}  {h.get('ticker'):20s}  "
                  f"local={h.get('stop_local')}  t212={h.get('stop_t212')}  "
                  f"delta={h.get('delta')}")

    else:
        # Default: initialise DB and report
        db = get_db()
        db.close()
        print(f"Database initialised at {DB_PATH}")
        print(f"Run with --migrate to import existing JSON state")
        print(f"Run with --status to view current state")
