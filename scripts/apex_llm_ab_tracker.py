#!/usr/bin/env python3
"""
LLM A/B Decision Tracker
Logs every LLM decision alongside what the rule-based baseline would have done.
When trades close (via apex-outcomes.json), links outcomes back to the decision
so we can measure whether the LLM actually improved P&L.

Tracked modules:
  preflight       — LLM may BLOCK a trade baseline would ALLOW
  signal_tiebreak — LLM may reorder signals baseline would leave ordered
  exit_timing     — LLM may adjust the partial-close fraction

File: apex-llm-ab-log.json
  {"records": [
    {"id": "preflight_ULVR_20260414T150217",
     "module": "preflight",
     "signal_name": "ULVR",
     "timestamp": "2026-04-14T15:02:17Z",
     "llm_decision": "BLOCK",
     "baseline_decision": "ALLOW",
     "llm_differed": true,
     "llm_reason": "CEO departure + earnings miss",
     "outcome_status": "pending",   # pending | win | loss | avoided | expired
     "outcome_pnl": null,
     "outcome_notes": null,
     "outcome_resolved_at": null
    }
  ]}

CLI:
    python3 apex_llm_ab_tracker.py status
    python3 apex_llm_ab_tracker.py resolve   (scan outcomes, link results)
    python3 apex_llm_ab_tracker.py summary   (monthly Telegram-ready report)
"""
import sys
import json
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

try:
    from apex_utils import locked_read_modify_write, safe_read, log_warning, send_telegram
except ImportError:
    def safe_read(p, d=None):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return d
    def log_warning(m): print(f'WARNING: {m}')
    def send_telegram(m): print(m)
    def locked_read_modify_write(path, fn, default=None):
        import tempfile
        try:
            data = safe_read(path, default)
            data = fn(data)
            d = os.path.dirname(path)
            with tempfile.NamedTemporaryFile(mode='w', dir=d, delete=False, suffix='.tmp') as tf:
                json.dump(data, tf, indent=2)
                tmp = tf.name
            os.replace(tmp, path)
        except Exception as e:
            print(f'ERROR: locked_read_modify_write failed: {e}')

AB_FILE       = '/home/ubuntu/.picoclaw/logs/apex-llm-ab-log.json'
OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
_MAX_RECORDS  = 1000   # keep at most this many records


def record_decision(
    module: str,
    signal_name: str,
    llm_decision: str,
    baseline_decision: str,
    llm_reason: str = '',
):
    """
    Log an LLM decision alongside the rule-based baseline.
    Non-blocking — never raises.

    Args:
        module:            'preflight' | 'signal_tiebreak' | 'exit_timing'
        signal_name:       instrument name (e.g. 'ULVR')
        llm_decision:      e.g. 'BLOCK' / 'ALLOW' / 'RERANKED' / 'KEPT' / '0.60'
        baseline_decision: what rule-based fallback would return
        llm_reason:        one-line LLM explanation
    """
    try:
        now = datetime.now(timezone.utc)
        ts  = now.strftime('%Y-%m-%dT%H:%M:%SZ')
        rid = f"{module}_{signal_name}_{now.strftime('%Y%m%dT%H%M%S')}"

        record = {
            'id':                 rid,
            'module':             module,
            'signal_name':        signal_name,
            'timestamp':          ts,
            'llm_decision':       str(llm_decision),
            'baseline_decision':  str(baseline_decision),
            'llm_differed':       str(llm_decision) != str(baseline_decision),
            'llm_reason':         str(llm_reason)[:150],
            'outcome_status':     'pending',
            'outcome_pnl':        None,
            'outcome_notes':      None,
            'outcome_resolved_at': None,
        }

        def _update(data):
            if not isinstance(data, dict):
                data = {}
            records = data.get('records', [])
            records.append(record)
            if len(records) > _MAX_RECORDS:
                records = records[-_MAX_RECORDS:]
            data['records'] = records
            return data

        locked_read_modify_write(AB_FILE, _update, default={})
    except Exception as _e:
        log_warning(f"apex_llm_ab_tracker: record_decision failed (non-blocking): {_e}")


def resolve_outcomes():
    """
    Scan apex-outcomes.json and link closed trades back to pending AB records.
    Call this whenever a trade closes (or daily from cron).

    For 'preflight BLOCK' decisions: the trade never happened.
    We use next-day price simulation if available, otherwise mark as 'avoided'.
    For other decisions: link actual trade P&L.
    """
    try:
        outcomes_data = safe_read(OUTCOMES_FILE, {})
        trades = outcomes_data.get('trades', [])
        if not trades:
            return

        # Build lookup: instrument name → most recent closed trade
        trade_map: dict = {}
        for t in trades:
            name = t.get('name', '')
            if name:
                trade_map[name] = t   # last occurrence wins (most recent)

        def _update(data):
            if not isinstance(data, dict):
                return data
            changed = False
            for r in data.get('records', []):
                if r.get('outcome_status') != 'pending':
                    continue
                name = r.get('signal_name', '')
                module = r.get('module', '')

                if module == 'preflight' and r.get('llm_decision') == 'BLOCK':
                    # LLM blocked this trade — find if a future trade in this instrument
                    # closed at a loss (would validate the block) or gain (false positive)
                    trade = trade_map.get(name)
                    if trade:
                        trade_ts = trade.get('closed_at') or trade.get('timestamp', '')
                        ab_ts    = r.get('timestamp', '')
                        if trade_ts > ab_ts:
                            pnl = float(trade.get('pnl', 0))
                            status = 'loss_avoided' if pnl < 0 else 'gain_missed'
                            r['outcome_status']     = status
                            r['outcome_pnl']        = pnl
                            r['outcome_notes']      = (
                                f"LLM blocked; subsequent trade returned £{pnl:.2f}"
                            )
                            r['outcome_resolved_at'] = datetime.now(timezone.utc).strftime(
                                '%Y-%m-%dT%H:%M:%SZ')
                            changed = True
                    else:
                        # Expire after 5 trading days with no matching outcome
                        ab_time = datetime.fromisoformat(
                            r.get('timestamp', '').replace('Z', '+00:00'))
                        if (datetime.now(timezone.utc) - ab_time).days > 5:
                            r['outcome_status']     = 'expired_no_trade'
                            r['outcome_resolved_at'] = datetime.now(timezone.utc).strftime(
                                '%Y-%m-%dT%H:%M:%SZ')
                            changed = True

                elif module in ('signal_tiebreak', 'exit_timing'):
                    trade = trade_map.get(name)
                    if trade and trade.get('pnl') is not None:
                        pnl    = float(trade.get('pnl', 0))
                        status = 'win' if pnl > 0 else 'loss'
                        r['outcome_status']     = status
                        r['outcome_pnl']        = pnl
                        r['outcome_resolved_at'] = datetime.now(timezone.utc).strftime(
                            '%Y-%m-%dT%H:%M:%SZ')
                        changed = True

            return data

        if trades:
            locked_read_modify_write(AB_FILE, _update, default={})

    except Exception as _e:
        log_warning(f"apex_llm_ab_tracker: resolve_outcomes failed: {_e}")


def format_summary(days: int = 30) -> str:
    """Return a Telegram-ready A/B performance summary for the last N days."""
    try:
        data    = safe_read(AB_FILE, {})
        records = data.get('records', [])
        cutoff  = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT')
        records = [r for r in records if r.get('timestamp', '') >= cutoff]

        if not records:
            return f'📊 LLM A/B: No records in last {days} days'

        total      = len(records)
        differed   = sum(1 for r in records if r.get('llm_differed'))
        resolved   = [r for r in records if r.get('outcome_status') != 'pending']
        pending    = total - len(resolved)

        # Preflight outcomes
        pf_blocks  = [r for r in records if r.get('module') == 'preflight'
                      and r.get('llm_decision') == 'BLOCK']
        pf_resolved= [r for r in pf_blocks if r.get('outcome_status') != 'pending']
        pf_avoided = sum(1 for r in pf_resolved if r.get('outcome_status') == 'loss_avoided')
        pf_missed  = sum(1 for r in pf_resolved if r.get('outcome_status') == 'gain_missed')
        pf_saved   = sum(abs(r.get('outcome_pnl', 0)) for r in pf_resolved
                         if r.get('outcome_status') == 'loss_avoided')

        lines = [f'📊 LLM A/B RESULTS ({days}d)\n']
        lines.append(f'  Total decisions: {total} | LLM differed: {differed} | Pending: {pending}')
        if pf_blocks:
            lines.append(f'\n  🛑 PREFLIGHT BLOCKS: {len(pf_blocks)}')
            if pf_resolved:
                lines.append(f'     ✅ Losses avoided: {pf_avoided} (est. £{pf_saved:.2f} saved)')
                lines.append(f'     ❌ Gains missed:   {pf_missed}')
                if len(pf_resolved) > 0:
                    accuracy = pf_avoided / len(pf_resolved) * 100
                    lines.append(f'     Accuracy: {accuracy:.0f}%')

        # Exit timing outcomes
        et_records = [r for r in records if r.get('module') == 'exit_timing'
                      and r.get('llm_differed')]
        if et_records:
            et_wins  = sum(1 for r in et_records if r.get('outcome_status') == 'win')
            et_total = len([r for r in et_records if r.get('outcome_status') != 'pending'])
            lines.append(f'\n  ⏱️ EXIT TIMING (LLM adjusted): {len(et_records)}')
            if et_total:
                lines.append(f'     Win rate: {et_wins}/{et_total} ({et_wins/et_total*100:.0f}%)')

        return '\n'.join(lines)

    except Exception as _e:
        return f'📊 LLM A/B: error generating summary ({_e})'


def get_module_performance(module: str, last_n: int = 20) -> str:
    """
    Return a compact plain-English summary of this module's recent LLM decision track record.
    Intended to be prepended to LLM prompts so the model can calibrate its reasoning based
    on how accurate its past decisions have been.

    Returns an empty string if there are no records or on any error.
    """
    try:
        data    = safe_read(AB_FILE, {})
        records = [r for r in data.get('records', []) if r.get('module') == module]
        if not records:
            return ''
        records = records[-last_n:]
        total   = len(records)

        if module == 'preflight':
            blocks          = [r for r in records if r.get('llm_decision') == 'BLOCK']
            resolved_blocks = [r for r in blocks  if r.get('outcome_status') != 'pending']
            avoided = sum(1 for r in resolved_blocks if r.get('outcome_status') == 'loss_avoided')
            missed  = sum(1 for r in resolved_blocks if r.get('outcome_status') == 'gain_missed')
            if not blocks:
                return (f"YOUR RECENT TRACK RECORD (last {total} decisions): "
                        "You have allowed every recent trade (no blocks issued). "
                        "If you see a genuine falling knife, it is safe to block it.")
            if not resolved_blocks:
                return (f"YOUR RECENT TRACK RECORD (last {total} decisions): "
                        f"You blocked {len(blocks)} trades, none have resolved yet (pending outcomes).")
            acc = avoided / len(resolved_blocks) * 100
            calibration = (
                "Your blocks have been accurate — continue at this threshold."
                if acc >= 60
                else "More gains were missed than losses avoided — raise your blocking bar, be more selective."
            )
            return (
                f"YOUR RECENT TRACK RECORD (last {total} decisions): "
                f"You blocked {len(blocks)} trades. Of {len(resolved_blocks)} resolved: "
                f"{avoided} losses avoided, {missed} gains missed ({acc:.0f}% block accuracy). "
                f"{calibration}"
            )

        elif module == 'signal_tiebreak':
            reranked          = [r for r in records if r.get('llm_decision') == 'RERANKED']
            resolved_reranked = [r for r in reranked if r.get('outcome_status') != 'pending']
            wins = sum(1 for r in resolved_reranked if r.get('outcome_status') == 'win')
            if not reranked:
                return ''
            if not resolved_reranked:
                return (f"YOUR RECENT TRACK RECORD (last {total} decisions): "
                        f"You reranked {len(reranked)} times, outcomes still pending.")
            wr = wins / len(resolved_reranked) * 100
            calibration = (
                "Your reranking has added value — continue applying qualitative judgment."
                if wr > 55
                else "Reranking has not consistently improved outcomes — prefer the numeric score order when close."
            )
            return (
                f"YOUR RECENT TRACK RECORD (last {total} decisions): "
                f"You reranked {len(reranked)} times. Reranked trade win rate: "
                f"{wins}/{len(resolved_reranked)} ({wr:.0f}%). {calibration}"
            )

        elif module == 'exit_timing':
            adjusted     = [r for r in records if r.get('llm_differed')]
            resolved_adj = [r for r in adjusted  if r.get('outcome_status') != 'pending']
            wins = sum(1 for r in resolved_adj if r.get('outcome_status') == 'win')
            if not adjusted:
                return ''
            if not resolved_adj:
                return (f"YOUR RECENT TRACK RECORD (last {total} decisions): "
                        f"You adjusted exit fractions {len(adjusted)} times, outcomes still pending.")
            wr = wins / len(resolved_adj) * 100
            return (
                f"YOUR RECENT TRACK RECORD (last {total} decisions): "
                f"You adjusted exit fractions {len(adjusted)} times. "
                f"Win rate on LLM-adjusted exits: {wr:.0f}%."
            )

        return ''

    except Exception:
        return ''


if __name__ == '__main__':
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else 'status'

    if cmd == 'status':
        data = safe_read(AB_FILE, {})
        records = data.get('records', [])
        pending = [r for r in records if r.get('outcome_status') == 'pending']
        print(f"Total records: {len(records)} | Pending: {len(pending)}")
        for r in records[-10:]:
            diff = '⚡' if r.get('llm_differed') else '  '
            print(f"  {diff} {r['timestamp'][:16]} {r['module']:15s} {r['signal_name']:8s} "
                  f"LLM={r['llm_decision']:6s} base={r['baseline_decision']:6s} "
                  f"→ {r.get('outcome_status','?')}")

    elif cmd == 'resolve':
        resolve_outcomes()
        print('✅ Outcomes resolved')

    elif cmd == 'summary':
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        print(format_summary(days))

    else:
        print('Usage: apex_llm_ab_tracker.py [status | resolve | summary [days]]')
