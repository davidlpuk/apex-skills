# APEX Trading System — Claude Context

> Read this before exploring any files. It replaces most exploratory reads.
> After making changes, update `CHANGES.md` and the relevant section below.

---

## Project Layout

```
/home/ubuntu/.picoclaw/
├── CLAUDE.md              ← this file (project root context)
├── CHANGES.md             ← chronological change log — read before starting work
├── dashboard/             ← Flask web app (port 7777)
│   ├── app.py             ← 3400-line main dashboard (Flask + inline SPA HTML)
│   ├── CLAUDE.md          ← dashboard-specific context
│   └── tax_tracker/       ← CGT Blueprint mounted at /tax/
│       ├── routes.py      ← 983 lines, all CGT logic + routes
│       ├── models.py      ← SQLAlchemy ORM (Trade, Instrument, S104Pool, etc.)
│       └── templates/tax_tracker/
│           ├── base.html  ← shared layout, CSS vars, Lucide SVGs, sort JS
│           ├── dashboard.html
│           ├── trades.html
│           ├── portfolio.html
│           ├── harvest.html
│           └── sa108.html
├── scripts/               ← 90+ Python/shell automation scripts
│   └── CLAUDE.md          ← scripts-specific context
└── logs/                  ← runtime JSON state files (DO NOT read unless asked)
```

---

## Service Management

```bash
sudo systemctl restart apex-dashboard   # after any Python change
sudo systemctl status apex-dashboard    # check running
sudo journalctl -u apex-dashboard -n 50 # view logs
```
Templates (`*.html`) are read from disk on every request — no restart needed for HTML-only changes.

---

## Dashboard (`app.py`) Architecture

- **Single file SPA**: all HTML/CSS/JS is one Python triple-quoted string `HTML = '''...'''`
- **Critical rule**: Never put triple single-quotes `'''` inside the HTML string — use `"""` or escape
- **JS escaping inside `HTML`**: Use `\\'` not `\'`, avoid `\\\"` in `onclick` attributes — use `null` instead of complex `querySelector` expressions
- **Auto-refresh**: every 60 seconds via `setInterval(updateClock,1000)` + `refreshSecs` counter
- **13 parallel API fetches**: `loadAll()` uses `Promise.all()` — any single failure aborts all rendering

### Page Structure
```
showPage(name, el)     — switches visible page div, sets nav-item.active
loadAll()              — fetches all 13 APIs, calls all render*() functions
renderOverview()       — stat-grid (g4, 7 cards), ov-regime, ov-autopilot, ov-health, ov-positions
renderPositions()      — full-positions table (14 cols), correlation matrix, P&L chart
renderSignals()        — pending signal, EV history, stats, recent list
renderRegime()         — regime detail, scaling bars, geo flags, market direction
renderPerformance()    — perf stats (g4), P&L chart, backtest detail, calendar heatmap
renderCalendarHeatmap()— 52-week GitHub-style daily P&L grid (data: portfolio.pnl_by_date)
renderAutopilot()      — ap-stats, ap-log
renderHealth()         — health-services, health-log, health-info
renderAlerts()         — sticky alerts-banner (position:sticky;top:0;z-index:50)
renderTaco()           — TACO classifier state machine
```

### API Endpoints
| Endpoint | Source files |
|---|---|
| `/api/portfolio` | `apex-positions.json`, `apex-outcomes.json`, Trading 212 live |
| `/api/regime` | `apex-regime.json`, `apex-regime-scaling.json`, `apex-geo-news.json` |
| `/api/signals` | `apex-trading-listener.log`, `apex-ev-log.json` |
| `/api/performance` | `apex-outcomes.json`, `apex-benchmark.json`, `apex-drawdown.json` |
| `/api/autopilot` | `apex-autopilot.json` |
| `/api/alerts` | `apex-circuit-breaker.json`, `apex-drawdown.json`, positions |
| `/api/health` | systemd + `apex-health.log` |
| `/api/macro` | `apex-macro-signals.json`, `apex-insider-data.json` |
| `/api/sectors` | `apex-sector-rotation.json`, `apex-breadth-thrust.json` |
| `/api/sentiment` | `apex-sentiment.json` |
| `/api/watchlist` | `apex-watchlist-analysis.json` |
| `/api/queue` | `apex-trade-queue.json` |
| `/api/taco` | `apex-taco-state.json`, `apex-taco-monitor-state.json` |

### CSS Design System
```css
--bg:#08090e; --surface:#0f1018; --surface2:#161722; --border:#1f2035;
--text:#e8e9f5; --muted:#5a5b7a; --accent:#6c63ff; --accent2:#a78bfa;
--green:#10d9a0; --red:#f56565; --amber:#f6ad55; --blue:#63b3ed;
```
Fonts: `Inter` (body/labels), `JetBrains Mono` (`.mono` class — numbers/prices/tickers)
Grid classes: `.g2` `.g3` `.g4` (CSS grid, 12px gap)
Sidebar: 230px fixed left, `max-width:1600px` main area

### Keyboard Shortcuts (added)
`O` P S R W H A Q M T = nav pages | `F5`/`Ctrl+R` = refresh | `Esc` = close sim panel | `?` = help toast

---

## Tax Tracker (`tax_tracker/`) Architecture

- **Blueprint**: registered at prefix `/tax/`, name `tax_tracker`
- **Database**: SQLite at `~/.picoclaw/data/apex-tax.db` (WAL mode, FK enabled)
- **HMRC matching rules** (in order): Same-Day → 30-Day B&B → S104 Pool
- **FX workflow**: USD trades need GBP rate confirmed before CGT calculations include them

### Key Routes
```
/tax/                  → dashboard (CGT Position)
/tax/trades/           → trade log with pagination (50/page) + filters
/tax/portfolio/        → S104 pool holdings with live valuations
/tax/harvest/          → loss harvesting opportunities
/tax/sa108/            → SA108 form output + CSV export
/tax/fx/pending/       → FX rate confirmation queue
/tax/import/apex/      → sync from apex-positions.json + apex-outcomes.json
/tax/recalculate/      → rebuild all CGT calculations from trades
```

### Key Helpers in routes.py
```python
_year_stats(session)           # returns {year: {taxable, has_pending_fx}} for all years
_summary_from_calcs(calcs, ty, yr)  # builds CGT summary dict
_load_match_results_for_year(session, yr)  # loads GainCalc records for a tax year
tax_year_bounds(yr)            # returns (start_date, end_date) for a UK tax year string
```

### Template Patterns
- Year tabs: `{% for y in all_years %}{% set ys = year_stats.get(y, {}) %}` — amber dot for pending FX, amount in green/amber
- Smart qty: `{{ '%.0f'|format(q) if q == q|int else '%.4f'|format(q) }}`
- Namespace sums: `{% set ns = namespace(v=0) %}{% for item in x %}{% set ns.v = ns.v + item.val %}{% endfor %}`
- FX pending rows: class `fx-pending` → amber left border via CSS

---

## Scripts Architecture

See `scripts/CLAUDE.md` for per-script documentation.

Core pattern: scripts write JSON to `logs/` then the dashboard reads those files via `load()` helper.

```python
# load() helper in app.py
def load(filename, default=None):
    path = os.path.join(LOG_DIR, filename)
    try:
        with open(path) as f: return json.load(f)
    except: return default or {}
```

LOG_DIR = `/home/ubuntu/.picoclaw/logs/`

---

## Performance Data Pipeline

```
apex-outcomes.json           → closed trades (pnl, dates, tickers)
  ↓ api_portfolio()
pnl_history[]                → cumulative P&L series (for line chart)
pnl_by_date{}                → daily P&L keyed by date string (for calendar heatmap)
  ↓ renderPerformance()
renderCalendarHeatmap()      → 52-week GitHub-style grid, green=gain, red=loss
```
With only 1 closed trade currently — charts show 1 data point. Will populate as trades close.

---

## Common Tasks (Token-Efficient Approach)

| Task | What to read | What NOT to read |
|------|-------------|-----------------|
| Fix dashboard JS | app.py lines ~1992–3300 | app.py CSS/HTML (lines 1363–1990) |
| Fix dashboard CSS | app.py lines ~1363–1475 | JS section |
| Fix tax tracker route | routes.py specific function | All templates |
| Fix template | Specific template file only | routes.py, models.py |
| Add API field | api_portfolio() ~line 110–192 | JS render functions |
| Add new page | HTML page div + render function + nav item | Other pages |
| Restart after Python change | `sudo systemctl restart apex-dashboard` | — |
