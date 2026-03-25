# Scripts Context

> See `/home/ubuntu/.picoclaw/CLAUDE.md` for full project context.
> See `/home/ubuntu/.picoclaw/CHANGES.md` for recent changes.

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
