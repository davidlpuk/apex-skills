# APEX Trading System — Claude Context

> Read this before exploring any files. It replaces most exploratory reads.
> After making changes, update `CHANGES.md` and the relevant section below.

---

## ⚠️ MARKET STATUS — ALWAYS CHECK, NEVER GUESS

**Never assume markets are open or closed based on the date or reasoning.**
Always read the live file:

```bash
cat /home/ubuntu/.picoclaw/logs/apex-market-calendar.json
```

Key fields:
- `today.status` — `OPEN` / `CLOSED_WEEKEND` / `CLOSED_HOLIDAY` / `US_CLOSED` / `UK_CLOSED`
- `today.uk_currently_open` — LSE open right now (08:00–16:30 UTC)
- `today.us_currently_open` — NYSE/NASDAQ open right now (14:30–21:00 UTC)
- `today.uk_holiday` / `today.us_holiday` — holiday name if closed

The file is updated every hour by cron. If it is >2h stale, run:
```bash
/home/ubuntu/bin/python3 /home/ubuntu/.picoclaw/scripts/apex-market-calendar.py
```

Holiday schedule is hardcoded in `apex-market-calendar.py` (US_HOLIDAYS, UK_HOLIDAYS dicts). Verify entries cover the current year when starting a new year.

---

## Project Layout

```
/home/ubuntu/.picoclaw/
├── CLAUDE.md              ← this file
├── CHANGES.md             ← chronological change log — read before starting work
├── dashboard/             ← Flask web app (port 7777)
│   ├── app.py             ← 3400-line main dashboard (Flask + inline SPA HTML)
│   ├── CLAUDE.md          ← dashboard line map, API endpoints, CSS vars
│   └── tax_tracker/       ← CGT Blueprint mounted at /tax/
│       ├── routes.py      ← 983 lines, all CGT logic + routes
│       ├── models.py      ← SQLAlchemy ORM (Trade, Instrument, S104Pool, etc.)
│       └── templates/tax_tracker/
├── scripts/               ← 90+ Python/shell automation scripts
│   └── CLAUDE.md          ← script index, coding standards, lessons learned
└── logs/                  ← runtime JSON state files (DO NOT read unless asked)
```

---

## Service Management

```bash
sudo systemctl restart apex-dashboard   # after any Python change
sudo systemctl status apex-dashboard    # check running
sudo journalctl -u apex-dashboard -n 50 # view logs
```
Templates (`*.html`) hot-reload — no restart needed for HTML-only changes.

---

## Dashboard (`app.py`) — Critical Rules Only

- **Single file SPA**: all HTML/CSS/JS is one triple-quoted string `HTML = '''...'''`
- **Never put `'''` inside the HTML string** — use `"""` or escape
- **JS escaping**: use `\\'` not `\'`; avoid `\\"` in `onclick` — use `null` instead of complex querySelector
- **13 parallel API fetches**: `loadAll()` uses `Promise.all()` — any single failure aborts all rendering
- See `dashboard/CLAUDE.md` for section map, API endpoints, CSS vars

---

## Tax Tracker (`tax_tracker/`) Architecture

- **Blueprint**: prefix `/tax/`, name `tax_tracker`
- **Database**: SQLite at `~/.picoclaw/data/apex-tax.db` (WAL mode, FK enabled)
- **HMRC matching** (in order): Same-Day → 30-Day B&B → S104 Pool
- **FX workflow**: USD trades need GBP rate confirmed before CGT calculations run

### Key Routes
```
/tax/              → CGT dashboard    /tax/trades/       → paginated trade log
/tax/portfolio/    → S104 pool        /tax/harvest/      → loss harvesting
/tax/sa108/        → SA108 + CSV      /tax/fx/pending/   → FX confirmation queue
/tax/import/apex/  → sync positions   /tax/recalculate/  → rebuild CGT
```

### Key Helpers (routes.py)
```python
_year_stats(session)                        # {year: {taxable, has_pending_fx}}
_summary_from_calcs(calcs, ty, yr)          # builds CGT summary dict
_load_match_results_for_year(session, yr)   # GainCalc records for a tax year
tax_year_bounds(yr)                         # (start_date, end_date) for UK tax year
```

### Template Patterns
- Smart qty: `{{ '%.0f'|format(q) if q == q|int else '%.4f'|format(q) }}`
- Namespace sums: `{% set ns = namespace(v=0) %}{% for i in x %}{% set ns.v = ns.v + i.val %}{% endfor %}`
- FX pending rows: class `fx-pending` → amber left border

---

## Scripts Architecture

Pattern: scripts read `../logs/*.json` or T212 API → compute → write `../logs/apex-<name>.json` → optional Telegram.
`LOG_DIR = /home/ubuntu/.picoclaw/logs/`
See `scripts/CLAUDE.md` for script index, cron schedule, and coding standards.

---

## Common Tasks (Token-Efficient Approach)

| Task | Read | Skip |
|------|------|------|
| Fix dashboard JS | app.py ~1992–3300 | CSS/HTML (1363–1990) |
| Fix dashboard CSS | app.py ~1363–1475 | JS section |
| Fix tax tracker route | routes.py specific fn | All templates |
| Fix template | Specific template only | routes.py, models.py |
| Add API field | api_portfolio() ~110–192 | JS render functions |
| Add new page | HTML div + render fn + nav | Other pages |
