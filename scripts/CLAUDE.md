# Scripts Context

> See `/home/ubuntu/.picoclaw/CLAUDE.md` for full project context.
> See `/home/ubuntu/.picoclaw/CHANGES.md` for recent changes.

## Agent-Native Tool Layer

```bash
python3 apex-tool-runner.py --list                # all tools (with tags)
python3 apex-tool-runner.py --describe <tool>     # inputs/outputs/safety
python3 apex-tool-runner.py --run <tool>          # run, get JSON back
python3 apex-tool-runner.py --run <tool> --force  # execute-trade only
```
**Safety levels:** `read` → `write-log` → `external-fetch` → `execute-signal` → `execute-trade`
Execute-trade tools return `{"status":"blocked"}` unless `--force`. Full map: `apex-tool-manifest.json`

## Architecture Pattern

All scripts: read `../logs/*.json` or T212 API → compute → write `../logs/apex-<name>.json` → optional Telegram.

## Script Index

Use `python3 apex-tool-runner.py --list` for the full live index (46 tools, tagged by category).

Key files to know by name:
- **Decision loop**: `apex-autopilot.py`, `apex-decision-engine.py`, `apex-contrarian-scan.py`
- **Execution**: `apex_order_executor.py`, `apex-trade-queue.py`, `apex-queue-revalidate.py`
- **Risk**: `apex-drawdown-check.py`, `apex-circuit-breaker.py`, `apex-atr-stops.py`
- **TACO**: `apex-taco-classifier.py`, `apex-taco-monitor.py`, `apex-taco-signal-injector.py`
- **Shared libs**: `apex_utils.py`, `apex_scoring.py`, `apex_filters.py`, `apex_sizer.py`, `apex_config.py`

## Key Config Files
- `apex_config.py` — thresholds, limits, API keys (via env)
- `apex-quality-universe.json` — stock universe for scanning
- `apex-ticker-map.json` — T212 ticker ↔ display name/currency mapping

## Cron Schedule (UTC)
`07:00` health/refresh · `07:25` breadth · `07:28` sentiment · `08:05` queue exec · `08:30` morning scan · `16:35` EOD

---

## Coding Standards & Lessons Learned

Production rules — follow when adding or modifying any script.

### Position Status Lifecycle — Both `pending` and `entry_placed` Must Be Cleaned Up
When a limit entry is placed, the position upgrades `pending` → `entry_placed`. If the limit is cancelled unfilled, `_remove_pending()` in `apex_order_executor.py` must clean up BOTH statuses. Only cleaning `pending` leaves a ghost `entry_placed` that confuses reconcile and triggers stale watchdog alerts for days. Fixed 2026-04-13.

### Reconcile Must Promote `entry_placed` → `protected`
Positions that exist in BOTH Apex (status=`entry_placed`) AND T212 are confirmed-filled but not yet acknowledged by Apex. Reconcile step 3 (added 2026-04-13) promotes them automatically and back-fills entry price from T212 `averagePrice`. Do not remove or skip this step.

### `save_positions` Race — Never Overwrite a Fresher `stop_order_id`
`apex-trailing-stop.py`'s `save_positions` merges by preferring its in-memory version. If broker-watchdog placed a new stop between the trailing stop's load and save, the merge would overwrite the new ID with a stale one. The merge now preserves the on-disk `stop_order_id` when it differs from memory. Fixed 2026-04-13.

### Reconcile Alert Read-Back — Only Read Outcome When One Was Logged
In `apex-reconcile.py`, the read-back of the last outcome trade must be inside the `else` block (only when `log_closed_position` was called). Reading it unconditionally causes unfilled ghost positions to display the previous trade's exit price in their alert. Fixed 2026-04-13.

### One Telegram Listener Only — `apex-trading-bot.service`
Two listener services (`apex-listener.service` and `apex-trading-bot.service`) cause every Telegram message to get two responses. `apex-listener.service` is disabled permanently. `apex-trading-bot.service` (running `apex-trading-listener.sh`) is the canonical listener. Do not re-enable the old service.

### Shell Scripts — Always Export PATH
Every `.sh` script invoked from cron must have `export PATH=/home/ubuntu/bin:$PATH` as line 2 (after `#!/bin/bash`). Cron's default `PATH` is `/usr/bin:/bin` — bare `python3` resolves to `/usr/bin/python3` which lacks venv packages (yfinance, etc.). This caused 71h of stale intelligence after a weekend run. Fixed 2026-04-13 across all 10 cron-invoked `.sh` scripts.
```bash
#!/bin/bash
export PATH=/home/ubuntu/bin:$PATH
```

### Log Severity — ERROR vs WARNING
`log_error` = human action required. `log_warning` = expected transient, system continues.
Health digest alerts at **>10 errors/24h** — transient noise masks real issues.

| Use `log_warning` | Use `log_error` |
|-------------------|----------------|
| External DNS/network timeout | Internal logic failure |
| T212 HTTP 429 (rate limit) | API returns unexpected structure |
| T212 HTTP 404 on DELETE (order already gone) | Stop placement failed, position unprotected |
| Optional data source unavailable (Reuters, FMP) | — |

### Scripts with Dedicated Log Files — No StreamHandler
Scripts that run as both cron jobs AND via `exec_module` (apex-fred-macro.py, apex-options-flow.py) must use **FileHandler only** — no StreamHandler. Cron redirects stdout to the same log file, causing double-logging.
```python
logging.basicConfig(handlers=[logging.FileHandler(LOG_FILE)])  # CORRECT
```

### exec_module Side Effects
`logging.basicConfig` at module level configures the **root logger** for the entire calling process when loaded via exec_module. Scripts loaded this way: apex-fred-macro.py, apex-options-flow.py, apex-macro-signals.py, apex-earnings-revision.py, apex-insider-data.py, apex-regime-scaling.py.

### State Cleanup on Guard Aborts
Guard checks that abort on stale state must also **delete** the stale file — not just return ABORT. Otherwise autopilot re-triggers every cycle. Fixed: STALE ABORT in apex-autopilot.py now deletes `apex-pending-signal.json`.

### Retry Policies — No Infinite Retry
Every retry loop must have a circuit breaker (max failures + cooldown). Example: broker-watchdog now uses 6h cooldown after 3 consecutive stop-placement failures, tracked in `apex-stop-fix-failures.json`.

### Local State vs API State — Reconciliation Required
JSON files mirroring API state must be cross-validated on a schedule. `apex-data-integrity.py` Check 6 runs every morning for stop price sync. Add a reconciliation check whenever adding new mirrored state.

### No `import` Inside Function Bodies
Python determines variable scope at compile time — `import x` inside a function makes `x` a local variable throughout, breaking `except` clauses that reference it before the import line. All imports at module level.

### Scanner Universe Validation
Tickers in INVERSE_UNIVERSE, WATCHLIST_YAHOO, and multiframe ticker maps can go stale (delisted/renamed). When 404/delisted errors start, search all of: `apex-inverse-scanner.py`, `apex-autopilot.py`, `apex-multiframe.py`, `apex-staleness-check.py`.

### T212 Ticker Map — LSE-Listed Equivalents for Leveraged/Inverse ETFs
US-listed leveraged/inverse ETFs are **MiFID II-blocked** for UK retail T212 accounts. Always use the LSE equivalent.

**Do not revert these mappings:**
| Instrument | Wrong (US) | Correct (LSE) |
|------------|-----------|--------------|
| 3x Short QQQ | `SQQQ_EQ` | `QQQSl_EQ` |
| 3x Short S&P | `SPXU_EQ` | `3ULSl_EQ` |
| Meta Platforms | `FB_US_EQ` | `META_US_EQ` |

When adding any new leveraged/inverse ETF, confirm a real T212 fill exists in `apex-outcomes.json` before scanning.

### T212 `instrument-invisible` — Permanent Block, Never Retry
HTTP 400 `{"type":"/api-errors/instrument-invisible"}` = T212 will never trade this instrument from this account. Fail immediately, send Telegram, do not retry.
```python
is_permanently_blocked = ('instrument-invisible' in stderr or
                          'instrument can not be traded' in stderr.lower())
```
`apex_utils` StreamHandler (WARNING+) ensures t212_request errors appear in subprocess stderr.
Contrast: 429/5xx = transient → retry with backoff. 400 instrument-invisible = permanent → cancel.

### Signal Generators Must Validate Numeric Fields Before Emitting
yfinance can return NaN during volatile markets. `atomic_write` converts NaN → null (valid JSON), but null entry/stop causes "Invalid payload" on execution. Guard every signal generator:
```python
import math
price = round(float(close.iloc[-1]), 2)
if not math.isfinite(price):
    return None   # skip — yfinance returned NaN
```
Fixed in `apex-earnings-drift.py:55` (2026-04-09).

### JSON Files Consumed as Dicts — Guard Against List Input
Files like `apex-earnings-flags.json` are initialised as `[]` when empty. Always guard before `.get()`:
```python
data = safe_read(FILE, {})
if not isinstance(data, dict):
    data = {}
```
Fixed in `apex-queue-revalidate.py:150` (2026-04-08).

### Duplicate Position Re-entry — Two-Layer Guard Required
`is_blocked()` must check **same t212_ticker already in positions**, not just total count. `apex-trade-queue.py` queue_signal() has a second-layer guard for slip-through. Both added 2026-04-10 after LEN/NFE were re-queued while already open.
```python
_held = {p.get('t212_ticker','') for p in intel.get('open_positions',[])}
if signal.get('t212_ticker','') in _held:
    blocks.append(f"Already in positions: {signal['t212_ticker']}")
```

### T212 Metadata Endpoint — Use List Fallback
`/equity/metadata/instruments/{ticker}` returns 404 (broken as of 2026-04-10). Use `/equity/metadata/instruments` (no suffix) to fetch all 16k instruments and cache. Pre-populated cache at `apex-instrument-meta.json`. The per-ticker endpoint path is dead — do not attempt to fix it.

### Double-Scan yfinance Rate Limiting
`apex-intraday-scan.sh` runs `apex-contrarian-scan.py` then immediately the decision engine calls `run_contrarian_scan()` which re-runs it. Second batch of 40 yfinance fetches gets rate-limited → 0 candidates → system idles. Fix: `run_contrarian_scan()` reads `apex-contrarian-signals.json` directly if file age < 20 min, falls through to subprocess only when stale. Added 2026-04-10.

### Idle System Diagnosis — Lead With cron.log
For "why are no trades executing": read `apex-cron.log` first. The decision engine prints its full reasoning there ("Found 0 contrarian candidates", "DEFENSIVE MODE", etc). Only read source if the log is ambiguous. Saves 10+ file reads.

### Late-Fill After Intraday Cancel — Always Check Portfolio Before Removing Pending
When an intraday limit order is cancelled after the 3-min non-fill window, a fill can arrive in
the final milliseconds and be processed by T212 **after** the DELETE request. If `_remove_pending`
runs on a position that T212 already holds, the position becomes "dark" — open in T212 with no
stop order and no `positions.json` entry. The watchdog catches it at the next cycle (30 min) but
cannot auto-fix it because no stop price is in positions.json.

Fix (in `apex_order_executor.py`): after the DELETE, fetch the T212 portfolio once. If the ticker
is present, it was a late fill — do NOT remove pending, update entry price/qty from T212, fall
through to stop placement as normal. Real-world example: ULVR 2026-04-13, 9.43 shares open with
no stop for ~20 minutes before manual intervention.

### GBX Instruments — T212 API Prices Are in Pence, Not Pounds
Signal prices (entry, stop, targets) are **always stored in pounds** (e.g. ULVR entry=£42.93).
T212's API for GBX-currency instruments (LSE stocks) expects prices in **pence** (GBX).
Always convert before API calls: `t212_price = price * 100 if currency == 'GBX' else price`.
This applies to: `limitPrice` (limit orders), `stopPrice` (stop orders), any other price fields.
Files fixed: `apex_order_executor.py`, `apex-broker-watchdog.py`, `apex-trailing-stop.py` (2026-04-13).
Use `_to_t212_price(price, currency)` helper function defined in all three files.
There are 30 GBX instruments in `apex-ticker-map.json` — the bug affects all of them if traded.

### FAILED Same-Day Dedup — Prevents Re-Queue Thrashing
`_is_duplicate()` in `apex-trade-queue.py` blocks QUEUED, EXECUTED, **and FAILED** same-day entries.
This prevents a failing signal from thrashing (re-queued every scan, fails again, repeat).
If the underlying cause is fixed (e.g. after a code change mid-day), manually set those entries
to CANCELLED to allow a fresh attempt.

### Queue Execute Lock — Prevent Overlapping Cron Invocations
`apex-trade-queue.py execute` wraps execution in a PID lock (`apex-queue-execute.lock`).
Lock file stores PID; stale locks auto-clear via `os.kill(old_pid, 0)` raising OSError when dead.
Fail-open on write error (execution proceeds). Without this, a 3-min poll window + stop placement
can cause two overlapping instances placing duplicate orders.

### Rollout Simulation Is a Hard Block, Not Advisory
`apex-rollout-sim.py` FAIL verdict (simulated WR < 30% OR day-1 stop probability > 30%) is a hard
block in `apex-decision-engine.py` — it returns immediately and sends a Telegram alert. WARN is
still advisory. Do NOT downgrade FAIL back to advisory — it means the risk structure is genuinely
poor and the simulator is telling you not to take the trade.

### Contrarian Scan Must Exclude Already-Held Positions
`apex-contrarian-scan.py run()` calls `_held_tickers()` which reads `apex-positions.json` and
returns names/tickers with active statuses. These are skipped during scanning. Without this,
a held position (e.g. ULVR, LEN, NFE) generates a contrarian signal every scan cycle, which then
fails at the `is_blocked()` duplicate guard — wasting yfinance fetches and polluting scan output.

### EV Marginal Signals Are Blocked in CAUTIOUS/HOSTILE Regimes
In adverse market regimes (CAUTIOUS, HOSTILE), MARGINAL EV signals (EV between -2 and 0) are
blocked in addition to the standard NEGATIVE gate. In normal regimes, marginal signals still pass
(the wide CI at low sample sizes means optimistic EV is still positive — worth taking the trade).
In CAUTIOUS/HOSTILE, the market is already penalising marginal setups — only positive EV trades.

### `str(None)` Returns `'None'` — Never Use `str()` to Guard Optional IDs
`str(None)` produces the non-empty string `'None'`, which passes `if not sid` and `if sid` guards.
This caused false STOP MISSING alerts: `stop_order_id=null` in JSON → Python `None` → `str(None)='None'`
→ `t212_stops.get('None')` returned `None` → triggered alert logic as if an ID existed but was unfound.
Always guard with `raw = p.get('stop_order_id'); sid = str(raw) if raw is not None else ''`.
Never do `sid = str(p.get('stop_order_id'))` — the result is always truthy even when the value is null.

### Multi-Venue Architecture — All T212-Specific Checks Need Venue Guards
When a second venue (e.g. Alpaca) is added, every function in the broker watchdog and trailing-stop
that calls a T212 API must skip non-T212 positions. Functions affected when Alpaca was added:
- `check_stale_in_flight` — queries T212 order status by ID; Alpaca UUIDs cause HTTP 400
- `check_deferred_stops` — places stops via T212; Alpaca positions handled by apex-alpaca-watchdog.py
- `check_stop_price_drift` — compares local stop against T212 stop order; meaningless for Alpaca
Pattern: add `if p.get('venue') == 'ALPACA': continue` at the top of each position loop.
When adding a third venue, grep for all `for p in positions` loops in watchdog/trailing-stop.

### GBX Stop-Price Drift — T212 Returns Pence, Positions File Stores Pounds
`check_stop_price_drift` in `apex-broker-watchdog.py` fetches stop orders from T212 and compares
against the local `stop` field. For GBX instruments T212 returns the stop in **pence**, but
positions.json stores it in pounds. Without conversion, a £41.26 stop appears as a 4084-unit drift
against the 4126p T212 value — triggers false STOP DRIFT alert AND overwrites positions.json with
the pence value (4126.0), corrupting the stop price for all downstream consumers.
Fix: `if currency == 'GBX' and t212_stop_raw > pos_stop * 10: t212_stop = round(t212_stop_raw / 100, 4)`
The `> pos_stop * 10` heuristic safely distinguishes pence from pounds without hardcoding a threshold.
