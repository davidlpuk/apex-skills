# APEX Change Log

> Read this at the start of every session to understand what has already been done.
> Append entries at the TOP (newest first). Format: `## YYYY-MM-DD — Description`

---

## 2026-03-25 — Score lift: uncertainty-aware Kelly, slippage model, layer audit

**Files changed:** `scripts/apex-kelly-v2.py`, `scripts/apex-expected-value.py`, `scripts/apex-layer-audit.py` (new)

### Changes

#### 1. Parameter Uncertainty Factor — Kelly v2
- New `parameter_uncertainty_factor(n, target_n=50)` function in `apex-kelly-v2.py`
- Formula: `max(0.10, n / 50)` — scales from 0.10 at n=0 to 1.0 at n=50 closed trades
- Applied to `f_adjusted` when `using_prior=True`: at n=0, sizing reduced to 10% of what
  prior-based Kelly would otherwise recommend (e.g. £51 instead of £516 on a £5k portfolio)
- Exposed in `adjustment_factors` dict as `uncertainty`, `uncertainty_n`, `uncertainty_target_n`
- Shown in verdict reason: `uncert×0.1 (n=0/50)` so it's visible in logs
- Motivation: at n=2 trades, 95% CI on win rate spans [0.03, 0.97]. Sizing at Kelly(prior_μ)
  as a point estimate ignores this enormous uncertainty. The factor enforces humility.

#### 2. Slippage Model — EV Calculator
- New `estimate_slippage(entry, quantity, atr, currency)` function in `apex-expected-value.py`
- ATR-based: `2 × 0.04 × ATR × qty` (4% of ATR per side, entry + exit)
- Fallback: `0.16%` round-trip of notional when no ATR available
- `calculate_ev()` now accepts `atr=` parameter; deducts `slippage_cost` in addition to
  `transaction_cost`. EV gate is now harder to pass — signals must overcome TC + slippage.
- Return dict adds: `slippage_cost`, `total_costs`, `slippage_atr_used`
- Display function shows slippage line separately

#### 3. Layer Correlation Audit — new script
- `apex-layer-audit.py` — reads `apex-decision-log.json`, extracts per-layer +1/-1/0
  contributions for every scored signal, computes pairwise Pearson correlations
- **First run result (13 signals, 11 layers):**
  - `BREADTH ↔ FRED: r=+1.000` — perfect redundancy (fire identically every session)
  - `GEO ↔ SENT: r=-1.000` — perfectly anti-correlated (cancel each other out)
  - `SECTOR ↔ GEO: r=+0.942`, `RS ↔ SECTOR: r=+0.836`
  - Effective dimensionality: **~5.3 from 11 layers (52% redundancy)**
- Output saved to `logs/apex-layer-audit.json`
- Re-run after adding signals: `python3 apex-layer-audit.py`
- Action required: BREADTH and FRED are identical in current market — consider whether
  they can be merged or one deactivated when they are tautologically correlated

---

## 2026-03-25 — Fix EV gate: prior reward discount + lower sample thresholds

**Files changed:** `scripts/apex-expected-value.py`, `scripts/apex-kelly-v2.py`

### Problem
EV model expected 2.6R per winning trade (T1 × 60% + T2 × 40% from signal targets).
Empirical average win: 0.22R (1 closed winner). Error: 1082%. Net effect: EV was
mathematically positive for every signal regardless of quality — the gate filtered nothing.

### Changes
- **`PRIOR_REWARD_DISCOUNT = 0.45`** applied to `avg_win_per_share` when empirical T1/T2
  sample < 5. Pulls expected win from ~2.6R down to ~1.17R, making EV gate meaningful.
  Discount is automatically removed once ≥5 empirical winning trades are recorded.
- **Win-rate sample threshold**: 5 → 3 (2 existing trades now inform empirical win rate)
- **T1/T2 split threshold**: 10 → 5 (empirical split activates sooner)
- **Kelly `MIN_TRADES_CONTINUOUS`**: 20 → 10 (Kelly switches from prior to real data sooner)
- T1/T2 label now shows `n=` so confidence level is visible in EV log

### Impact
Signals that previously always showed POSITIVE EV now show MARGINAL or NEGATIVE until
empirical data confirms the edge. EV gate is now a real filter, not a rubber stamp.

---

## 2026-03-25 — Data Engineering Scorecard: 5 Additional Fixes

**Files changed:** `scripts/apex-fundamental-signals.py`, `scripts/apex_scoring.py`, `scripts/apex_order_executor.py`

### Changes made

#### Fix 2: Insider signal FMP quota bypass closed
- `get_insider_signal()` in `apex-fundamental-signals.py` now calls `fmp_request()` instead of direct `urllib.request.urlopen()` — ensures all insider-trading API calls are counted in the shared `apex-fmp-quota.json` quota tracker (was invisible: ~21 calls/week)

#### Fix 3: Sector rotation + breadth staleness gate
- `apex_scoring.py` — sector boost layer now checks `intel['file_ages_hours']['sector_rotation']` and `breadth`; skips and logs if either is >24h old (seen 118h stale in smoke test)

#### Fix 4: Backtest insights TTL warning
- `apex_scoring.py` — if `apex-backtest-v2-insights.json` is >7 days old, appends `BACKTEST-WARN` to signal adjustments prompting a re-run of `apex-backtest-v2.py`

#### Fix 5: Entry > stop validation before execution
- `apex_order_executor.py` — rejects orders where `stop >= entry` before any API call; logs error + sends Telegram alert

---

## 2026-03-25 — Elite Trader Scorecard: 3 Recommendations Implemented

**Files changed:** `scripts/apex-regime-realtime.py` (new), `scripts/apex-alpaca-executor.py` (new), `scripts/apex_order_executor.py`, `dashboard/app.py`

### Changes made

#### 1. Real-Time Regime Updates (Recommendation #2)
- **`apex-regime-realtime.py`** — New polling daemon: fetches VIX every 5 min, full 30-stock breadth recalc every 30 min, updates `apex-regime.json` + triggers `apex-regime-scaling.py` recalc automatically
- VIX move ≥2pts → Telegram alert; VIX ≥35 → BLOCKED alert
- Market hours aware (07:00–18:00 UTC Mon–Fri only)
- **`apex-regime-realtime.service`** — systemd service, enabled and running
- Regime now reflects intraday VIX moves (e.g. first poll: VIX 26.95 → 25.01 detected in real time)

#### 2. Alpaca Execution Upgrade (Recommendation #1)
- **`apex-alpaca-executor.py`** — New module: Alpaca v2 REST API wrapper for US stock order placement (limit, market, stop, GTC). Paper/live mode via `ALPACA_LIVE=true` in `.env.trading212`.
- **`apex_order_executor.py`** — US stocks (35 tickers in `_ALPACA_US_TICKERS`) now route to Alpaca first when credentials are configured; T212 remains fallback for UK/EU stocks and when Alpaca is unavailable
- Activation: add `ALPACA_API_KEY=` and `ALPACA_SECRET=` to `~/.picoclaw/.env.trading212`
- Test: `python3 apex-alpaca-executor.py --test`

#### 3. Statistical Significance Progress Widget (Recommendation #3)
- **`dashboard/app.py`** — New `renderStatSignificance()` function + `#stat-significance` card on Performance page
- Shows: live trade count vs 50-trade target, progress bar, confidence tier (Hypothesis → Reliable → Statistical), Wilson score 95% confidence interval on win rate, pace estimate (days to 50 trades), milestone unlocks (10/25/50/100 trades)
- Turns green at 50 trades, amber at 25, red below

---

## 2026-03-25 — Main Dashboard 15-Issue UX Overhaul + Calendar Heatmap

**Files changed:** `dashboard/app.py`

### Changes made
1. **Refresh button loading state** — `loadAll()` now disables the button and shows "⟳ Loading…" during fetch, re-enables in `finally` block. Button has `id="refresh-btn"`.
2. **Autopilot buttons replaced** — Enable/Disable/Pause buttons were toast-only (did nothing). Replaced with an instruction box showing the Telegram commands (`AUTOPILOT ON` / `AUTOPILOT OFF` / `APEX PAUSE`).
3. **Overview positions table** — Trimmed from 10 columns to 6: Instrument (+ ticker sub-line), P&L, R, Stop Distance, T1 Progress, Action. Empty state colspan updated to 6.
4. **Stat grid split** — "Positions / Autopilot" card split into two: **Open Positions** (count + total book risk) and **Trade Budget** (trades today/max + autopilot mode). Stat grid changed from `.g3` to `.g4` (now 7 cards, 4+3 layout).
5. **Drawdown shows £ absolute** — Sub-line now: `NORMAL · £0 · 100% sizing`. Computed as `|dd_peak - dd_current|`.
6. **Overview Regime card simplified** — Now shows: regime label (large) + RAG pill (GREEN/AMBER/RED based on scale%) + one-liner "VIX 18 · Breadth 62% · Scale 80%" + "Full details →" link. Full details still on Regime page.
7. **Alerts banner sticky** — Added `position:sticky;top:0;z-index:50;background:var(--bg)` so HALT/critical alerts stay pinned regardless of scroll or active page.
8. **Lucide SVG icons** — All 15 unicode sidebar symbols (◈▦◎⊕▤◐◧⊛◉⊠◑⊙♡⊞🌮) replaced with proper Lucide SVG icons. `.nav-icon` CSS updated to flex display.
9. **Keyboard shortcuts** — `O` P S R W H A Q M T = navigate pages, `F5`/`Ctrl+R` = reload data, `Esc` = close sim panel, `?` = help toast.
10. **max-width** — 1180px → 1600px (better use of wide monitors).
11. **Watchlist hover** — `#wl-tbody tr:hover td` gets `rgba(108,99,255,0.08)` background.
12. **Grid texture** — Body `::before` pseudo-element opacity 0.02 → 0.01 (less visual noise).
13. **Performance benchmarks** — Sharpe colour-coded (green ≥1.0, amber ≥0.5, red <0.5) + "target >1.0" sub-label. Win Rate sub: "break-even ~40%". Drawdown sub: "max X%".
14. **Calendar heatmap** — New 52-week GitHub-style daily P&L heatmap on Performance page. Data from new `pnl_by_date` field added to `/api/portfolio` response. Green = gain, red = loss, intensity scales relative to max gain/loss. Hover shows date + P&L. Summary strip: active days, gain/loss day counts, total P&L.
15. **Bug fix** — `onclick` in Overview Regime card had `\\\"` escaping that rendered as `\"` in HTML, terminating the attribute early and causing a complete JS parse failure ("Loading…" forever). Fixed to `showPage('regime',null)`. Also simplified keyboard shortcut `querySelector` expression.

### API changes
- `GET /api/portfolio` now returns `pnl_by_date: {date_str: daily_pnl}` in addition to existing fields.

---

## 2026-03-24 — CGT Tax Tracker Full UX Overhaul

**Files changed:** `dashboard/tax_tracker/routes.py`, `dashboard/tax_tracker/templates/tax_tracker/base.html`, `dashboard.html`, `trades.html`, `portfolio.html`, `harvest.html`, `sa108.html`

### Changes made
- **base.html**: Full rewrite — Inter + JetBrains Mono dual-font system, Lucide SVG nav icons, mobile hamburger + overlay, table sort engine (date/num/text), pagination CSS, year-tab two-line layout, FX pending row amber left-border, `.btn-disabled` class, wider progress bars.
- **routes.py**: Added `_year_stats()` helper, `calc_map` for recent sells, pagination on trades route (50/page), `year_stats` passed to dashboard/trades/sa108 routes.
- **dashboard.html**: Year tabs with taxable amount + FX pending dot, AEA card above breakdown, Recalculate button loading state, Recent Disposals gain/loss column.
- **trades.html**: Pagination controls, "Showing X–Y of Z" counter, FX pending rows with amber badge, sort headers.
- **portfolio.html**: Sort headers, Rebuild Pools loading state, tfoot totals row with colour coding.
- **harvest.html**: Added 5th stat card "Gains After Harvest" (shows net position after crystallising all losses).
- **sa108.html**: Export blocked uses `.btn-disabled` with inline explanation text instead of btn-danger.

---

## 2026-03-19 — Black Swan & War-Response Hardening

**Files changed:** `scripts/apex-blackswan.py`, `scripts/apex-autopilot.py`, various

- 10 improvements to black swan detection and war/geopolitical response logic.
- See git log: `172f02a`

---

## Earlier Work

See `git log` for commits prior to 2026-03-19.
