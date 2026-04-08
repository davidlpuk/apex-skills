# Scripts Context

> See `/home/ubuntu/.picoclaw/CLAUDE.md` for full project context.
> See `/home/ubuntu/.picoclaw/CHANGES.md` for recent changes.

## Agent-Native Tool Layer

To invoke any script as an agent (Claude, cron, future agents):

```bash
python3 apex-tool-runner.py --list                # all 46 tools
python3 apex-tool-runner.py --list --tag risk     # filter by tag
python3 apex-tool-runner.py --describe <tool>     # inputs/outputs/safety
python3 apex-tool-runner.py --run <tool>          # run, get JSON back
python3 apex-tool-runner.py --run <tool> --force  # execute-trade only
```

**Safety levels:** `read` → `write-log` → `external-fetch` → `execute-signal` → `execute-trade`
Execute-trade tools (broker-watchdog, trailing-stop, etc.) return `{"status":"blocked"}` unless `--force`.
Full capability map: `apex-tool-manifest.json`

## Architecture Pattern

All scripts follow one pattern:
1. Read input from `../logs/*.json` or Trading 212 API
2. Compute/analyse
3. Write output JSON to `../logs/apex-<name>.json`
4. Optionally send Telegram message via `apex-telegram.sh`

## Script Categories

### Core Engine
| Script | Output | Runs |
|--------|--------|------|
| `apex-autopilot.py` | `apex-autopilot.json` | Every scan |
| `apex-decision-engine.py` | `apex-decision-log.json` | Every scan |
| `apex-contrarian-scan.py` | `apex-contrarian-signals.json` | Morning |
| `apex-regime-check.py` | `apex-regime.json` | Morning |
| `apex-regime-scaling.py` | `apex-regime-scaling.json` | Morning |
| `apex-drawdown-check.py` | `apex-drawdown.json` | Every scan |
| `apex-circuit-breaker.py` | `apex-circuit-breaker.json` | Every scan |

### Market Data
| Script | Output |
|--------|--------|
| `apex-market-data.py` | prices |
| `apex-market-direction.py` | `apex-market-direction.json` |
| `apex-breadth-thrust.py` | `apex-breadth-thrust.json` |
| `apex-sector-rotation.py` | sector data |
| `apex-vix-correlation.py` | `apex-vix-correlation.json` |

### Signals & Scoring
| Script | Purpose |
|--------|---------|
| `apex-expected-value.py` | EV calculation per signal |
| `apex-score-adapter.py` | 18-layer score → trade decision |
| `apex-multiframe.py` | Multi-timeframe confirmation |
| `apex-sentiment.py` | VADER NLP sentiment |
| `apex-fundamentals.py` | PE/EPS/quality metrics |
| `apex-macro-signals.py` | FRED macro data |
| `apex-insider-edgar.py` | EDGAR insider trading data |
| `apex-options-flow.py` | Options flow signals |

### Risk & Position Management
| Script | Purpose |
|--------|---------|
| `apex-atr-stops.py` | ATR-based stop calculation |
| `apex-position-sizer.py` | Kelly/fixed-fraction sizing |
| `apex-correlation-check.py` | Portfolio correlation check |
| `apex-drawdown-check.py` | Peak-to-trough drawdown |
| `apex-partial-close.py` | Partial position closure |
| `apex-trailing-stop.py` | Trailing stop updates |

### TACO (Trump Always Chickens Out)
| Script | Purpose |
|--------|---------|
| `apex-taco-classifier.py` | VIX spike classifier |
| `apex-taco-monitor.py` | State machine monitor |
| `apex-taco-signal-injector.py` | Trade signal injection on walkback |
| `apex-taco-outcomes-tracker.py` | Outcome tracking |

### Shared Libraries (`apex_*.py`)
| File | Contains |
|------|---------|
| `apex_config.py` | Config loading, constants |
| `apex_utils.py` | Shared utilities |
| `apex_scoring.py` | 18-layer scoring system |
| `apex_filters.py` | Signal filters |
| `apex_sizer.py` | Position sizing |
| `apex_order_executor.py` | T212 order execution |
| `apex_price_feed.py` | Price feed abstraction |
| `apex_intelligence.py` | Intelligence aggregation |
| `apex_market_calendar.py` | Trading calendar |

## Key Config Files
- `apex_config.py` — main config (thresholds, limits, API keys via env)
- `apex-quality-universe.json` — stock universe for scanning
- `apex-ticker-map.json` — T212 ticker → display name mapping

## Cron Schedule
See `apex-autopilot.json` for current schedule. Key times (UTC):
- `07:00` — health check, data refresh
- `07:25` — market direction, breadth
- `07:28` — sentiment
- `08:05` — queue execution (trades queued outside market hours)
- `08:30` — morning scan
- `16:35` — EOD review

---

## Coding Standards & Lessons Learned

These rules come from production incidents. Follow them when adding or modifying scripts.

### Log Severity — ERROR vs WARNING
**Rule:** `log_error` means "human action required". `log_warning` means "expected transient failure, system continues".

| Situation | Use |
|-----------|-----|
| External DNS / network timeout | `log_warning` |
| T212 HTTP 429 (rate limit) | `log_warning` (already retrying) |
| T212 HTTP 404 on DELETE | `log_warning` (order already gone = expected) |
| Optional data source unavailable (Reuters, FMP) | `log_warning` |
| Internal logic failure | `log_error` |
| API returns unexpected structure | `log_error` |
| Stop placement failed, position unprotected | `log_error` |

Inflating ERROR count with transient network noise masks real issues and triggers false health alerts. The health digest alerts at >10 errors/24h.

### Scripts with Dedicated Log Files — No StreamHandler
**Rule:** If a script uses `logging.basicConfig(handlers=[FileHandler(...)])`, do **not** also add `StreamHandler()`.

**Why:** Several scripts (apex-fred-macro.py, apex-options-flow.py) run as both cron jobs AND are loaded inline via `exec_module` from apex_scoring.py / apex-decision-engine.py. The cron redirects stdout to the same log file (`>> apex-fred-macro.log 2>&1`). Adding `StreamHandler` causes every log line to be written twice — once by FileHandler, once via stdout redirect.

**Pattern:**
```python
# CORRECT — dedicated log file scripts
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE)]
)

# WRONG — causes double-logging when run via cron
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
```

### exec_module Side Effects
**Rule:** Any script that uses `logging.basicConfig` at module level has a side effect when loaded via `exec_module` — it configures the root logger for the entire calling process. This can cause other scripts' log messages to appear in the wrong log file.

Scripts currently loaded via exec_module in scoring/decision engine: apex-fred-macro.py, apex-options-flow.py, apex-macro-signals.py, apex-earnings-revision.py, apex-insider-data.py, apex-regime-scaling.py, and others. Only FRED and options-flow have their own logging setup — fixed to FileHandler-only.

### State Cleanup on Guard Aborts
**Rule:** When a guard check blocks an operation on stale state (e.g., staleness check on a pending signal), it must also **remove the stale state**, not just return ABORT.

**Why:** "Block and notify" leaves the stale file in place forever. The autopilot will keep generating ABORT telegrams every cycle until someone manually deletes the file. Fixed in apex-autopilot.py: STALE ABORT now deletes `apex-pending-signal.json`.

### Retry Policies — No Infinite Retry
**Rule:** Every retry loop must have a circuit breaker: maximum consecutive failures + cooldown period. Never retry an API call indefinitely.

**Why:** SPYLs_EQ stop placement failed with 400 Bad Request (T212 may not support GTC stops on this instrument). The watchdog retried every 30 minutes, generating repeated errors and masking the real issue. Fixed with 6h cooldown after 3 consecutive failures, tracked in `apex-stop-fix-failures.json`.

### Local State vs API State — Reconciliation Required
**Rule:** Any local JSON file that mirrors API state (e.g., `apex-positions.json` mirroring T212 positions and orders) must be cross-validated against the live API on a regular schedule.

**Why:** The AAPL stop order silently diverged — positions.json said 239.74 but T212 had 233.11. All R-multiple calculations, Kelly sizing, and drawdown estimates used the wrong stop for an unknown period. No script caught this until a manual audit.

The `apex-data-integrity.py` now runs Check 6 (stop price sync) every morning. If adding new local state that mirrors external state, add a corresponding reconciliation check.

### No `import` Inside Function Bodies
**Rule:** All imports belong at module level. Never write `import sys` (or any module) inside a function body.

**Why:** Python determines variable scope at compile time for the entire function. An `import sys` anywhere in a function makes Python treat `sys` as a local variable throughout that function — including in `except:` clauses before the import is reached, causing `UnboundLocalError`. Found in apex-staleness-check.py.

### Scanner Universe Validation
**Rule:** Tickers in INVERSE_UNIVERSE, WATCHLIST_YAHOO, and multiframe ticker maps must be validated against Yahoo Finance periodically. Delisted/renamed instruments cause silent 404 errors on every scan.

**Why:** 3USS.L (WisdomTree S&P 500 3x Short) was delisted/renamed on Yahoo Finance but remained in three separate files (apex-inverse-scanner.py, apex-autopilot.py, apex-multiframe.py), generating errors on every multiframe and inverse scanner run. The FRED log (root logger pollution from exec_module) was the only place these errors appeared, making them hard to find.

When a ticker starts returning 404/delisted errors from yfinance, search all of: INVERSE_UNIVERSE, WATCHLIST_YAHOO, multiframe ticker maps, and apex-staleness-check.py WATCHLIST_YAHOO.
