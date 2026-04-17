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

### Reconcile Must NOT Double-Log Partial Closures via auto_reconciled
When a position closes via T1 partial → stop on remainder → reconciler discovers
it's gone from T212, the executor logs the partial outcome row first, then the
reconciler tries to log ANOTHER row with `outcome_type=auto_reconciled_not_in_t212`.
That second row uses the **original non-decremented `quantity`** from positions.json
against the **most recent T212 sell price** — counting the same shares twice.

Real-world example (2026-04-16): XOM partial closed via T1 +£28.90 (qty=2 sold).
Reconciler then logged auto_reconciled +£59.24 using qty=4 × stop fill price,
inflating realized P&L by £30. Across 4 positions (XOM, VUAG, QQQSl, ABBV) the
inflation was £63 — visible to the user as a fictional dashboard P&L of £181 vs
real T212 account growth of £33.

Fix: `log_closed_position()` in `apex-reconcile.py` now skips the
`auto_reconciled_not_in_t212` write if an outcome row already exists for the
same `(ticker, opened)` pair. The structural fix (decrement positions.json
`quantity` after each partial fill) is still pending — until then this dedup
guard prevents the symptom.

Related rule: **dashboard P&L headline must come from T212's `/equity/account/cash`,
never from `apex-outcomes.json` sums.** outcomes.json is gross/pre-fees and can be
polluted by issues like this; T212's `result` field is the broker's net truth.

### outcomes.json is for Analytics, NOT a Cash Ledger
`apex-outcomes.json` exists for win-rate, R-multiple bucketing, MAE/MFE, edge-proof
DSR — it is not a cash ledger and was never reconciled to broker statements. The
`pnl` field is **gross** (no fees, no FX spread, no slippage subtracted), and the
file is vulnerable to double-logging on partial-close-then-stop sequences. When you
need to display "how much money has the strategy made" use T212's `/equity/account/cash`
`result` field. When you need "what's the win rate of CONTRARIAN signals" use
outcomes.json. Mixing the two is what produced the 2026-04-16 inflated-P&L outage.

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

### GBX Fix Must Be Applied to Every Script That Compares T212 Prices vs Local Values
When fixing a GBX pence/pounds mismatch in one script, **audit all other scripts** that make the same
comparison. In 2026-04-13, `apex-broker-watchdog.py` received the fix but `apex-data-integrity.py`
(Check 6: stop price sync) was missed — it continued logging false STOP DRIFT warnings until 2026-04-14.
Scripts that compare T212 stop/price values against local positions.json values: broker-watchdog, data-integrity.
Pattern: search for `t212_stop\|stopPrice\|t212_stp` when applying any GBX price fix.

### Stop Tighten Must Validate Price and Market Hours Before Cancelling Existing Stop
When the agent tightens a stop (`apex-atr-stops.py` / `mcp__apex__tighten_stop`), it cancels the
existing stop order and places a new one. Two guards are required before doing so:

1. **Price validity**: new stop price must be strictly below current market price (for longs).
   If the proposed stop ≥ current price, T212 rejects with `"owned: 0.0"` (not an obvious message).
   Guard: `if new_stop >= current_price: skip — would immediately trigger or be rejected`.

2. **Market hours**: T212 will not accept new stop orders for US stocks outside 14:30–21:00 UTC.
   If the market is closed, do NOT cancel the existing stop — leave it in place and defer the
   tighten until market open. Cancelling first and placing second creates an unprotected window
   that can last hours if placement keeps failing.

Real-world example (2026-04-14): agent tightened ABBV stop to £205.5 at 09:47 UTC (market closed,
current price £205.27). Old stop cancelled. New stop rejected by T212. Position unprotected for ~5h.
Broker watchdog entered 6h cooldown masking the real cause. Fix: validate both before cancelling.

### T212 `"owned: 0.0"` on Stop Placement = Price Invalid or Market Closed (Not Instrument Block)
HTTP 400 `{"type":"/api-errors/selling-equity-not-owned","detail":"...owned: 0.0"}` during stop
placement does NOT mean the position is missing or instrument-blocked (unlike `instrument-invisible`).
It means T212 cannot accept the sell order at this moment — either:
- The stop price is at or above current market price (would immediately trigger), OR
- The US market is closed and T212 won't accept new GTC stop orders outside hours.
Distinguish from `instrument-invisible` (permanent) — this error is transient and resolves at open.
Broker watchdog cooldown should be set to clear at market open, not a fixed 6h window.

### Broker Watchdog Cooldown Should Target Market Open, Not a Fixed 6h Window
When stop placement fails with a market-hours error, the 6h fixed cooldown is wrong — it may
expire before or long after market open, causing either missed retries or unnecessary alerts.
On US stock stop failures outside hours, set `cooldown_until` to 14:25 UTC (5 min before open)
so the next watchdog cycle after open retries immediately.
Check `apex-market-calendar.json → today.us_currently_open` before placing or tightening stops.

### Venue Guards Must Use Both Venue Flag AND ID Format — `venue: null` Is Not Safe
`if p.get('venue') == 'ALPACA': continue` only skips positions where venue is explicitly set.
Positions created before multi-venue support, or with a bug that omitted the venue field, have
`venue: null` — they fall through the guard. If such a position has a UUID entry_order_id (Alpaca
style), `check_and_place_deferred_stops` will query T212 with the UUID and get HTTP 400.
Fix: add a secondary UUID-format guard after the venue check:
```python
if '-' in str(entry_id):   # UUID = Alpaca; T212 expects numeric Long IDs
    continue
```
Applied to `check_and_place_deferred_stops` in `apex-broker-watchdog.py` (2026-04-14).
General rule: whenever skipping non-T212 API calls, guard on BOTH `venue == 'ALPACA'` AND
ID format — defence in depth against missing/stale venue fields.

### `check_stop_price_drift` Must Skip (Not False-Alert) When Orders API Returns None
When `get_open_orders()` returns None (T212 rate-limited or Cloudflare-blocked), passing
`orders=None` to `check_stop_price_drift` previously collapsed to `live_orders=[]`. This made
every stop appear missing and triggered false "STOP MISSING" alerts for positions with valid
stops in T212. The root cause was `None or [] → []`.

Fix: distinguish API failure (None) from genuinely empty orders list:
```python
if orders is not None:
    live_orders = orders
else:
    _fetched = t212_request('/equity/orders', timeout=10)
    if _fetched is None:
        log_warning("... skipping drift check to avoid false STOP MISSING alerts")
        return []
    live_orders = _fetched if isinstance(_fetched, list) else []
```
Never use `(t212_request(...) or [])` for safety-critical checks — None means API unavailable,
which requires a different response than an empty list.
Applied to `apex-broker-watchdog.py:check_stop_price_drift` (2026-04-16).

### T212 Cloudflare Rate-Limit (Geo-1010) — Burst API Calls Trigger IP Block
Sending >60-90 T212 API calls in a ~10-15 minute window triggers Cloudflare's IP-based block
(HTTP 400/403, "error code: 1010"). The existing `User-Agent: Mozilla/5.0` in `apex_utils` helps
but does not prevent volume-based throttling. The block typically clears in 10-15 minutes.

**When does this happen:**
- 18 fill-polls × 10s per execution attempt = 18 T212 calls in 3 minutes
- Multiple execution attempts in quick succession multiply this
- BLK was attempted 3× in one hour (54+ polls) before the market-hours fix

**Mitigation:**
- Reduce polling burst: consider 15-20s poll interval instead of 10s (fewer calls per 3 minutes)
- If T212 returns "error code: 1010", back off 10-15 minutes before retrying
- Do not retry in a tight loop on 400/403 from Cloudflare — it makes the block worse

**Do not confuse with:**
- HTTP 429 TooManyRequests (T212's own rate limiter) — handled with `_t212_rate_limit()` in apex_utils
- HTTP 400 instrument-invisible — permanent instrument block, not Cloudflare

### Alpaca Is Disabled — All Trades Go Through T212 Only
`_ALPACA_AVAILABLE = False` is hardcoded in `apex_order_executor.py`. Do NOT re-enable.
The system detected Alpaca credentials in `.env.alpaca` and auto-routed all US stocks to
Alpaca paper trading. The user never wanted this — they only use T212. The result was XOM
being bought 9× in Alpaca (23.97 shares, ~$3,600) with zero visibility in T212.
If Alpaca routing is re-enabled in the future, the reconciler ghost detection (below) must
also be updated to exclude Alpaca-venue positions from the T212 ghost check.

### Reconciler Must Exclude Non-T212 Venues from Ghost Detection
`apex-reconcile.py` ghost detection computes `apex_tickers - t212_tickers`. Any position with
`venue=ALPACA` will never appear in `t212_tickers`, so it is always treated as a T212 ghost and
removed every reconcile cycle. This makes Alpaca positions "dark" — still held in Alpaca with no
Apex tracking, no stop, and no duplicate guard. In practice this caused XOM to be re-bought 9×
(23.97 shares, ~$3,600) because the duplicate-check in `is_blocked()` never fired.
Fix: exclude non-T212 venues before computing the ghost set:
```python
_non_t212 = {p.get('t212_ticker','') for p in apex_positions
             if p.get('venue') not in (None, '', 'T212')}
ghost_tickers = apex_tickers - t212_tickers - {''} - _non_t212
```
Applied to `apex-reconcile.py` (2026-04-16). When adding a third venue, this set must be updated.

### Alpaca Fractional Stop Orders Must Use DAY Time-in-Force, Not GTC
Alpaca rejects fractional-quantity stop orders with `time_in_force=gtc`:
`HTTP 422: {"code":42210000,"message":"fractional orders must be DAY orders"}`
Fix: use `day` TIF when `qty != int(qty)`, `gtc` only for whole-share quantities:
```python
is_fractional = (qty != int(qty))
tif = 'day' if is_fractional else 'gtc'
```
Implication: fractional day-stops expire at market close each day. The Alpaca watchdog must
re-place them at the next market open. The watchdog already re-runs `*/5 14-20 UTC` so it will
pick up `unprotected` Alpaca positions and re-place stops at 14:30 open.
Applied to `place_stop_order()` in `apex-alpaca-executor.py` (2026-04-16).

### Alpaca Watchdog Must Handle `unprotected` Positions, Not Just `awaiting_fill`
`apex-alpaca-watchdog.py` originally only processed `status=awaiting_fill` positions. Positions
reconstructed after a reconcile wipe, or any position whose stop was not placed at fill time,
have `status=unprotected` with no `stop_order_id`. These are never given stops.
Fix: added a second pass in `run()` that iterates positions where `venue=ALPACA`,
`status=unprotected`, and `stop_order_id` is None — and places a stop for each.
Applied to `apex-alpaca-watchdog.py` (2026-04-16).

### Position Sizer NAV Caps Must Be Scaled for Portfolio Size
`apex_sizer.py` `_VERDICT_NAV_CAP` defines the maximum position as a % of NAV per verdict tier.
Original values (PROVEN=2%, NOT_PROVEN=0.5%) were calibrated for >£10k portfolios. On a £4,634
portfolio: NOT_PROVEN cap = 0.5% × £4,634 = £23 — below `MIN_VIABLE_NOTIONAL` of £100. Every
NOT_PROVEN signal was silently blocked: sizer returned (0, 0), decision engine wrote qty=0 to
pending signal, executor looped on "Signal file incomplete" forever.
Fix: NOT_PROVEN raised to 1.5%, MIN_VIABLE_NOTIONAL lowered to £25 (T212 fractional shares).
Always verify: `NOT_PROVEN_CAP × NAV > MIN_VIABLE_NOTIONAL` before deploying with a new NAV.

### Decision Engine Must Guard Against qty=0 Return from Sizer
`calculate_final_position()` returns `(0, 0)` when any sizing gate blocks (edge-proof cap, Kelly
abort, below minimum viable size). Without a guard, the decision engine writes `quantity=0` to
`apex-pending-signal.json`. The executor reads this, finds no quantity, logs "Signal file
incomplete", and the pending file is never cleared — triggering the same error on every cron run.
Fix: after `calculate_final_position()`, check `if not qty or not notional: return` with a
Telegram alert explaining which gate triggered. Applied to `apex-decision-engine.py` (2026-04-16).

### Contrarian Scan Double Pence Conversion Locks Out All LSE Stocks
`apex-contrarian-scan.py` builds the `close` series with `.apply(fix_pence)` — already in pounds.
The original code then called `fix_pence(close.max(), currency)` again on `high_52`/`low_52`.
For GBX instruments this divided by 100 twice: SHEL.L £33 → £0.36 → `discount_pct = -9202%`.
All LSE/GBX stocks scored 1–3 vs 5–7 for USD stocks and could never win signal selection.
Fix: remove the second `fix_pence()` call — `close` is already in pounds after `.apply()`.
Never call `fix_pence()` on a value derived from a series that was already converted.
Applied to `apex-contrarian-scan.py` (2026-04-16). Also see `apex-market-data.py` — same pattern.

### TREND Scan Weight Variables Must Be Defined Before `get_technicals()`
`apex-market-data.py` uses `WEIGHT_TREND`, `WEIGHT_RSI`, `WEIGHT_VOLUME`, `WEIGHT_MACD` inside
`get_technicals()`. The original code defined these variables at the bottom of the file (line 279+)
after the main scanning loop. Every stock threw `NameError: name 'WEIGHT_TREND' is not defined`,
was logged as `"error": "name 'WEIGHT_TREND' is not defined"`, and was discarded. The TREND
strategy produced 0 signals for an unknown period.
Fix: move the weight loading block (file read + fallback) to before `get_technicals()`.
Rule: module-level variables used inside functions must be defined before the function is called,
not just before the end of the file. Python resolves names at call time, not parse time, but if
the call is at module level after the function definition, the order still matters.
Applied to `apex-market-data.py` (2026-04-16).

### Quality Check Name Resolution — Empty String Is Not the Same as Missing Key
`contrarian_quality_check()` in `apex-autopilot.py` used `signal.get('ticker', name)` to find the
key in the quality universe. If the signal dict has a `ticker` key present but empty (`"ticker":""`),
`signal.get('ticker', name)` returns `""` (the empty string), not the fallback `name`. Then
`"" in quality` = False → false QUALITY BLOCK for any instrument with an empty ticker field.
Fix: use a 4-layer resolution: `name` → stripped `t212_ticker` → display name match → `None`.
Guard every `.get(key, default)` fallback where the key may exist but be empty.
Applied to `apex-autopilot.py` (2026-04-16).

### Contrarian Scanner — Overbought RSI Instruments Score Via Discount Alone
The contrarian algorithm awards points for: RSI oversold + discount from 52w high + quality +
MACD turning. An instrument that crashed hard (e.g. 47% discount) but has since recovered
(RSI 86) can score 6/10 via discount + quality + MACD, despite being overbought — the opposite
of a contrarian entry. This causes the scanner to propose buying into an overextended bounce.
Fix: MACD bonus is suppressed when `rsi > 75` — the recovery has already run too far.
```python
if macd_rising and macd_hist > -0.5 and rsi <= 75:
    score += 1
    reasons.append("MACD turning — early reversal signal")
elif rsi > 75:
    reasons.append(f"RSI {rsi} — overbought, contrarian MACD bonus suppressed")
```
Applied to `apex-contrarian-scan.py` (2026-04-16). RSI > 75 still allows the discount and quality
bonuses — total score capped at 5 in that scenario, below the 6+ threshold.

### Market Hours Must Be Enforced at Every Layer — Not Just Score Adjustments
The +1/-1 venue scoring in `score_signal_with_intelligence()` is NOT sufficient to prevent
closed-market signals from being selected. At 11:02 UTC, BLK (USD) scored highest and was
selected despite NYSE being closed until 14:30 UTC. The executor then placed a limit order
that could never fill, polled for 3 minutes, and cancelled — wasting an execution slot.

Three hard gates are required (defence in depth):
1. **Decision engine [6/7] filter**: reject USD signals when `us_currently_open=false`,
   GBX/GBP when `uk_currently_open=false`. Uses calendar file read once per run.
2. **Order executor pre-Step-1**: abort execution and remove pending if market closed.
   This catches any signal that slipped through the decision engine (manual runs, replays).
3. **Autopilot**: check market hours before calling executor. Hold signal (don't delete)
   so it executes when the market opens rather than being wasted.

Currency → exchange mapping:
- USD → NYSE/NASDAQ → `us_currently_open`
- GBX, GBP → LSE → `uk_currently_open`
- EUR, CHF → European (similar hours to LSE) → `uk_currently_open`

### Fill-Poll Count × Interval Must Stay Below T212's Cloudflare Burst Ceiling
T212 sits behind Cloudflare which enforces a per-IP burst limit. Empirically, ~18+ T212 calls
in a ~10–15 min window can trigger error code 1010 (HTTP 400/403). The fill-poll loop is the
biggest single contributor to burst rate — it fires once per poll inside the 3-min wait window
for every limit order. Original config (18 polls × 10 s) hit the ceiling reliably; reduced to
9 × 20 s (same total wait, half the calls) on 2026-04-16. Do not increase
`T212_FILL_POLL_COUNT` above 9 without a corresponding increase to `T212_FILL_POLL_INTERVAL`
to keep `count × (60 / interval) ≤ 27` calls/min.

### Deferred-Stop Watchdog Spawn Must Be Delayed (Don't Stack Bursts)
`apex_order_executor.py` spawns `apex-broker-watchdog.py` in the background after deferring a
stop, so a quick late fill gets a stop placed without waiting 30 min for the next cron cycle.
The watchdog issues 5–10 T212 API calls itself. If it fires immediately after the fill-poll
burst, the two bursts stack — Cloudflare sees ~18+ calls/min and trips the 1010 block.
Fix: spawn via `bash -c 'sleep 90 && python apex-broker-watchdog.py'` so the burst counter
decays first. 90 s is empirically safe and still well within the late-fill-detection window.

### Edge-Proof Verdict Requires BOTH Win-Rate Significance AND Deflated Sharpe
`apex-edge-proof.py` graduates a strategy to CONFIRMED only when BOTH gates pass:
1. Win-rate p-value clears Benjamini-Hochberg FDR @ 10% across the family of strategies
2. Deflated Sharpe Ratio probability ≥ 0.95 (selection-bias + non-Gaussian adjusted)

The DSR `n_trials` parameter equals the number of strategies being tested in parallel
(currently `len(_TYPE_ALIASES) = 5`). When adding a new signal type, the DSR bar
automatically rises for ALL strategies — adding strategies makes "PROVEN" harder, not
easier. Do not lower `_BH_FDR` below 0.10 or relax the DSR threshold to chase verdicts.
The whole point is that a strategy that doesn't clear these bars is statistically
indistinguishable from luck.

### Never Mix Backtest and Real Trades 1:1 in Edge Proof
Backtest data carries look-ahead bias, regime-shift drift, and silent assumption
leakage; it should never outweigh real-trade evidence. `apex-edge-proof.py` uses
real-trade-dominant pooling: 1 real trade = 10 backtest trades, backtest capped at 3×
real-trade weight, backtest dropped entirely at `n_real ≥ 20`.
The original 30%-flat weighting let 372 backtest trades drown out 4 real trades — the
edge-proof verdict was effectively a backtest-only result with a real-trade fig leaf.
If you change the constants, rerun the edge-proof against `apex-outcomes.json` and
verify INSUFFICIENT_DATA verdicts don't silently flip to NOT_PROVEN/MARGINAL.

### `apex_price_feed` Yahoo-Symbol Resolution — Never Pass T212 Tickers Straight to yfinance
`get_live_price(ticker)` and `get_technical_data(ticker)` originally fell through to
`yf.Ticker(yahoo_ticker or ticker)` — the **raw** ticker, not the cleaned name. For LSE
instruments like `INRGl_EQ` the fallback queried Yahoo for literal `INRGL_EQ` → 404, polluting
the cron log and silently returning None so staleness/drift checks passed without ever
validating the price.

Fixed 2026-04-16: added `_YAHOO_MAP` (mirrors `WATCHLIST_YAHOO` in `apex-staleness-check.py`)
and a `_resolve_yahoo(clean, ticker)` helper. Both functions now call the resolver before
hitting yfinance:
- If the cleaned name is in the map (e.g. `INRG → INRG.L`), use that.
- If the raw ticker is already a Yahoo symbol (contains `.`, no `_`), accept it.
- Otherwise return `(None, "USD", "NO_YAHOO_MAP")` instead of making a 404 call.

When adding a new instrument: add it to BOTH `_YAHOO_MAP` in `apex_price_feed.py` AND
`WATCHLIST_YAHOO` in `apex-staleness-check.py`. Future cleanup: extract the shared map into
a single source of truth.

Note: `apex-price-feed.py` (hyphenated) is dead code — Python cannot `import` a hyphenated
module. The only live module is `apex_price_feed.py` (underscore).

### LSE-Listed Foreign-Currency ETFs Are Hard-Blocked at the Executor (No FX Layer)
The TREND/CONTRARIAN signal generators tag instruments with a `currency` from
`apex-market-data.py`'s WATCHLIST. That tag has historically disagreed with
**both** yfinance's quote currency AND T212's trading currency for non-GBP
LSE ETFs:

| Ticker | WATCHLIST tag | yfinance returns | T212 trades | Mismatch path |
|--------|---------------|------------------|-------------|---------------|
| VAPX.L | CHF | GBP | CHF | yfinance GBP → T212 CHF (no conversion) |
| HEAL.L | EUR | USD | EUR | yfinance USD → T212 EUR (no conversion) |
| IUCD.L | GBP | USD | USD | yfinance USD → T212 USD ✓ but tag wrong |
| VAGS.L | GBP | GBP | GBP | all three match — safe |
| DFNG.L | GBP | GBP | GBP | (was missing from ticker-map; added 2026-04-16) |

Without an FX layer that converts yfinance-quoted entry prices to T212's local
trading currency at order-submission time, the limit price is in the wrong
unit and never crosses the spread — wasting 9 fill polls × 20 s per attempt
and burning Cloudflare burst quota.

Fix in `apex_order_executor.py` (2026-04-16): pre-flight currency guard reads
`apex-ticker-map.json` for the **T212-side** trading currency (ground truth)
and aborts if the T212 ticker has an LSE suffix (`l_EQ`, `m_EQ`, `s_EQ`,
`d_EQ`) AND its T212 currency is not GBP/GBX. US-listed tickers (`_US_EQ`)
are never blocked — yfinance and T212 both use USD by construction.

When a true FX layer is added (multiply signal entry by GBP→target-currency
spot rate before submission), the guard can be loosened. Until then, do NOT
add USD/EUR/CHF LSE ETFs to the TREND/CONTRARIAN universes — the executor
will silently block them with a Telegram alert.

Companion fix: `_check_entry_staleness` now appends `.L` for **any** LSE T212
suffix, not just GBP/GBX-tagged ones. Without this, `HEALm_EQ` resolved to
bare `HEAL` on yfinance (the unrelated US REIT @ $25.86) and produced a
false +185.22% drift abort.

### Ticker-Map Currency = T212 Ground Truth, Signal Currency = Possibly Wrong
When making any decision in the executor that depends on the instrument's
trading currency (limit price unit, market-hours gate, GBX pence conversion,
FX snapshot, outcomes log), look the value up from `apex-ticker-map.json` by
matching `t212` to the signal's `t212_ticker`. **Do not trust** the signal's
`currency` field — the upstream WATCHLIST has been wrong for at least three
non-GBP LSE ETFs since the system's inception. The executor (2026-04-16) now
overrides `signal.currency` with the ticker-map value before downstream branches
fire.

### When Adding a New Instrument to Any Watchlist, Add to Ticker-Map FIRST
DFNG was added to `apex-market-data.py`'s WATCHLIST as `("DFNG.L", "GBP")` but
NOT to `apex-ticker-map.json`. Result: scoring + signal-generation worked, but
the signal's `t212_ticker` field was empty (the cleaning step couldn't find a
T212 mapping), and the executor errored on "Signal missing ticker or quantity"
every time DFNG ranked high. Three places must agree for any new instrument:
1. `apex-ticker-map.json` (yahoo_key → {t212, name, currency})
2. The relevant scanner WATCHLIST (`apex-market-data.py`, `apex-contrarian-scan.py`)
3. `_YAHOO_MAP` in `apex_price_feed.py` and `WATCHLIST_YAHOO` in `apex-staleness-check.py`
   if the instrument needs a non-default Yahoo symbol.

Future hardening: signal-generation should refuse to emit a signal whose
`t212_ticker` is empty/None — fail at the source, not in the executor.

### Limit-Price Slippage Premium for Illiquid ETFs (VAGS-class instruments)
A passive BUY limit posted at the inside ask never crosses the spread for
wide-spread instruments — VAGS bond ETF sat NEW for 9 polls × 20 s without a
single fill on 2026-04-16 even though the ETF was actively quoted on LSE.

Fix: `apex_order_executor.py` adds a small premium to the limit price before
submission, turning a passive limit into a "marketable limit" — still capped
(no runaway market fill) but priced through the inside ask.
- Standard premium: `T212_LIMIT_PREMIUM_BPS = 15` (0.15%)
- Illiquid override: `T212_LIMIT_PREMIUM_BPS_ILLIQUID = 35` (0.35%) for tickers
  in `T212_ILLIQUID_TICKERS` (mostly bond + commodity ETFs)
- Hard cap: `T212_LIMIT_PREMIUM_MAX_FRAC_OF_STOP = 0.5` — premium can never
  widen the entry by more than half the entry-to-stop distance, ensuring the
  entry never opens already inside the stop's risk envelope.

When fills repeatedly fail at the standard premium for a new instrument, add
its T212 ticker to `T212_ILLIQUID_TICKERS` in `apex_config.py`.

### FX Layer — `convert_price()` for non-GBP LSE ETFs (CHF/EUR/USD)
`apex_utils.convert_price(price, from_currency, to_currency)` provides a
GBP-base FX layer with a 6h-TTL file cache (`apex-fx-rates.json`). It composes
any pair via GBP (e.g. USD→GBP→EUR), handles GBX as GBP, and raises
`FxRateUnavailable` if no rate can be obtained — the executor must fail-CLOSED
on this exception (never submit a wrongly-priced limit).

Architecture rules:
1. **Ticker-map is the source of truth for both sides of the FX pair**:
   - `currency` = T212 trading currency (used for API submissions)
   - `yahoo_currency` = yfinance quote currency (signal-source unit)
   - When adding any LSE-listed non-GBP instrument, populate BOTH fields. The
     audit script in 2026-04-16 work fills `yahoo_currency` for 99 entries.
2. **Pre-flight FX validation in `execute()`**: read both currencies, and if
   they differ, run `convert_price()` once to fail fast before any T212 call.
3. **Apply FX mutation AFTER staleness check, BEFORE pending write**:
   - Staleness check uses original yfinance-currency entry vs yfinance live
     price (correct comparison)
   - Pending write stores T212-currency values so all downstream consumers
     (broker watchdog, trailing stop, drift check) see consistent units
4. **Post-FX sanity check**: `if stop >= entry: abort`. FX rate jitter
   between signal generation and execution can collapse a tight stop above
   entry. Submitting such a stop causes T212 to immediately market-sell the
   position. (HEAL was lost this way on 2026-04-16 in the first FX iteration
   that kept positions.json in yfinance currency.)

### Positions.json Stop Unit Invariant — Always T212 Trading Currency
`positions.json` `stop`, `entry`, `target1`, `target2` must be in the
**instrument's T212 trading currency** post-FX. Reasons:
- Broker watchdog (`apex-broker-watchdog.py`) reads `stop` and submits to T212
  unmodified — T212 interprets in trading currency
- Drift check compares positions.json `stop` to T212 `stopPrice` directly
- Trailing stop ratchet logic compares against current price (will be FX-aware
  in a future iteration but still needs T212 units to submit)

Two real-world bugs in the first FX iteration on 2026-04-16:
- VAPX (CHF): stop=28.37 in GBP → watchdog placed as 28.37 CHF → 11% wide
  instead of intended 6% (over-protected, not catastrophic)
- HEAL (EUR): stop=8.46 in USD → watchdog placed as 8.46 EUR → ABOVE entry
  of 7.61 EUR → T212 immediately market-sold (lost the position)

Fix: convert before pending write. The unit invariant is enforced by ordering
in `apex_order_executor.py` — FX conversion is the last step before Step 0
pending write.

### When Adding a Third FX Pair (e.g. JPY, CAD, AUD)
The cache layer (`_FX_GBP_PAIRS` in `apex_utils.py`) already lists JPY/CAD/
AUD/CNY but no LSE-listed instruments use them today. To add an instrument
in a new currency:
1. Verify the pair exists in `_FX_GBP_PAIRS` — if not, add the yfinance
   symbol (`GBP<XYZ>=X`)
2. Add the instrument to `apex-ticker-map.json` with both `currency` (T212)
   and `yahoo_currency` (yfinance) populated
3. Add to the relevant scanner WATCHLIST
4. Run `python3 -c "from apex_utils import convert_price; print(convert_price(100, 'GBP', 'XYZ'))"`
   to confirm the cache populates and returns a sensible rate
5. Add to the market-hours mapping in `apex_order_executor.py` if the
   instrument trades on a non-LSE exchange (currently EUR/CHF map to UK hours
   because all such instruments are LSE-listed)

### Currency Field Audit Script Pattern (yfinance vs ticker-map)
When ticker-map currencies drift out of sync with yfinance reality, the audit
pattern that worked on 2026-04-16:
```python
# For each ticker-map entry, fetch yf.Ticker(yahoo_symbol).fast_info.currency
# Compare against ticker-map currency. Cross-listed equities (BT, AVIVA, SIE,
# NOVN, ROG, AMD, PFE, PEP) need yahoo_ticker overrides because the bare LSE
# .L suffix is unreliable — use the German (.DE) or Swiss (.SW) listing's symbol.
```
Re-run this audit when:
- Adding new LSE-listed foreign-currency ETFs to the watchlists
- Yahoo Finance changes a primary listing (rare but happens)
- Suspected silent FX mismatches (a stream of "no fill in 9 polls" with
  correct entry prices is a strong signal)
