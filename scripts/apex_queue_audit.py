#!/usr/bin/env python3
"""
Apex Queue Audit Log — Phase 1 observability module.
Records every state transition in the signal lifecycle:
  None → QUEUED → (pending → entry_placed → protected/unprotected/REMOVED)
                → EXECUTED / FAILED / CANCELLED

Appended to apex-queue-audit.jsonl (one JSON object per line).
File-append only — never overwrites, no atomic_write needed.
"""
import json
import os
from datetime import datetime, timezone

AUDIT_FILE = '/home/ubuntu/.picoclaw/logs/apex-queue-audit.jsonl'


def record_transition(
    signal_id,
    ticker: str,
    signal_type: str,
    from_state,
    to_state: str,
    detail: str = '',
    t212_order_id=None,
    filled_qty: float = 0.0,
) -> None:
    """
    Append one transition record to the audit log.

    Args:
        signal_id:     Queue entry ID (int) or None if pre-queue
        ticker:        T212 ticker string e.g. 'XOM_US_EQ'
        signal_type:   e.g. 'CONTRARIAN', 'GEO_REVERSAL'
        from_state:    Previous state string or None
        to_state:      New state string
        detail:        Human-readable reason for the transition
        t212_order_id: T212 order ID if available (limit/stop order)
        filled_qty:    Shares filled at this transition (if applicable)
    """
    record = {
        'ts':             datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'signal_id':      signal_id,
        'ticker':         ticker,
        'signal_type':    signal_type,
        'from':           from_state,
        'to':             to_state,
        'detail':         detail,
        't212_order_id':  t212_order_id,
        'filled_qty':     filled_qty,
    }
    try:
        with open(AUDIT_FILE, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass  # Audit log is non-critical — silent failure, never blocks trading
