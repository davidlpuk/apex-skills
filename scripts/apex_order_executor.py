#!/usr/bin/env python3
"""
Apex Order Executor
Pure-Python replacement for apex-execute-order.sh.

Logic:
  Step 0: Write status="pending" to positions BEFORE any API call
  Step 1: Place limit entry order (falls back to market on failure)
  Step 2: Upgrade to status="entry_placed"
  Step 3: Place GTC stop-loss order (3 attempts, rate-limited)
  Step 4a: On stop success  → upgrade to status="protected"
  Step 4b: On stop failure  → upgrade to status="unprotected", alert
  Step 5: Telegram trade confirmation
  Step 6: Slippage check (non-blocking)

All T212 API calls go through t212_request() (rate limiter + retry).
All position writes go through locked_read_modify_write() (file locking).
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
try:
    from apex_utils import (
        safe_read, atomic_write, log_error, log_warning,
        locked_read_modify_write, t212_request, send_telegram,
        get_fx_rate, convert_price, FxRateUnavailable,
    )
except ImportError as _e:
    print(f"FATAL: apex_utils not available — {_e}")
    sys.exit(2)

try:
    from apex_config import (
        T212_FILL_POLL_COUNT, T212_FILL_POLL_INTERVAL, SIGNAL_MAX_AGE_HOURS,
        T212_LIMIT_PREMIUM_BPS, T212_LIMIT_PREMIUM_BPS_ILLIQUID,
        T212_LIMIT_PREMIUM_MAX_FRAC_OF_STOP, T212_ILLIQUID_TICKERS,
    )
except ImportError:
    T212_FILL_POLL_COUNT                = 18
    T212_FILL_POLL_INTERVAL             = 10
    SIGNAL_MAX_AGE_HOURS                = 6
    T212_LIMIT_PREMIUM_BPS              = 15
    T212_LIMIT_PREMIUM_BPS_ILLIQUID     = 35
    T212_LIMIT_PREMIUM_MAX_FRAC_OF_STOP = 0.5
    T212_ILLIQUID_TICKERS               = set()

try:
    from apex_queue_audit import record_transition as _audit
except ImportError:
    def _audit(*a, **kw): pass

SIGNAL_FILE         = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
PROCESSING_FILE     = SIGNAL_FILE + '.processing'
POSITIONS_FILE      = '/home/ubuntu/.picoclaw/logs/apex-positions.json'
LOG                 = '/home/ubuntu/.picoclaw/logs/apex-orders.log'
INSTRUMENT_META_CACHE = '/home/ubuntu/.picoclaw/logs/apex-instrument-meta.json'
REGIME_FILE         = '/home/ubuntu/.picoclaw/logs/apex-regime.json'


def _regime_snapshot():
    """
    Capture market regime at position-open time. Stored on the position so the
    eventual outcome row can be bucketed by entry regime (strategy×regime
    heatmap, edge-by-regime DSR). Never raises — missing file → empty dict.
    """
    try:
        with open(REGIME_FILE) as _f:
            r = json.load(_f)
        if not isinstance(r, dict):
            return {}
        return {
            'overall':         r.get('overall'),
            'vix_regime':      r.get('vix_regime'),
            'breadth_regime':  r.get('breadth_regime'),
            'vix':             r.get('vix'),
            'breadth_pct':     r.get('breadth_pct'),
        }
    except Exception:
        return {}


def _get_quantity_precision(ticker: str) -> int:
    """
    Return the number of decimal places T212 allows for this instrument.
    Fetches from T212 metadata API and caches indefinitely (instrument
    precision rules don't change).  Falls back to 2 on any error.

    T212 returns quantityPrecision as an integer (0 = whole numbers only,
    2 = up to 2 decimal places, etc.).
    """
    try:
        cache = safe_read(INSTRUMENT_META_CACHE, {})
        if ticker in cache:
            return int(cache[ticker].get('quantity_precision', 2))

        # Individual-ticker endpoint (/equity/metadata/instruments/{ticker}) is no longer
        # available on T212 — returns 404. Fall back to the list endpoint and cache all.
        data = t212_request(f'/equity/metadata/instruments/{ticker}')
        if data and isinstance(data, dict):
            precision = int(data.get('quantityPrecision', 2))
            cache[ticker] = {
                'quantity_precision': precision,
                'min_quantity':       data.get('minTradeQuantity'),
                'max_quantity':       data.get('maxTradeQuantity'),
            }
            atomic_write(INSTRUMENT_META_CACHE, cache)
            return precision

        # Fallback: fetch full instrument list and extract this ticker
        all_instruments = t212_request('/equity/metadata/instruments')
        if all_instruments and isinstance(all_instruments, list):
            for inst in all_instruments:
                t = inst.get('ticker', '')
                if t:
                    cache[t] = {
                        'quantity_precision': int(inst.get('quantityPrecision', 2)),
                        'min_quantity':       inst.get('minTradeQuantity'),
                        'max_quantity':       inst.get('maxTradeQuantity'),
                    }
            atomic_write(INSTRUMENT_META_CACHE, cache)
            if ticker in cache:
                return int(cache[ticker]['quantity_precision'])
    except Exception as _e:
        log_warning(f"Could not fetch instrument metadata for {ticker}: {_e} — defaulting to 2dp")
    return 2


def _round_quantity(qty: float, ticker: str) -> float:
    """Round quantity to T212's required precision for this instrument."""
    precision = _get_quantity_precision(ticker)
    rounded = round(qty, precision)
    if precision == 0:
        rounded = int(rounded)
    return rounded
TRADING_STATE  = '/home/ubuntu/.picoclaw/workspace/skills/apex-trading/TRADING_STATE.md'

# Alpaca routing DISABLED — all trades go through T212 only.
# To re-enable: set "use_alpaca": true in apex-autopilot.json
_alpaca_mod = None
_ALPACA_AVAILABLE = False

# US tickers that qualify for Alpaca execution (from apex-alpaca.py)
_ALPACA_US_TICKERS = {
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","CRM","ORCL",
    "AMD","INTC","QCOM","JPM","GS","MS","BAC","BLK","AXP","C","V",
    "JNJ","PFE","MRK","UNH","ABBV","TMO","DHR","KO","PEP","MCD",
    "WMT","PG","XOM","CVX","NVO"
}


_AUTOPILOT_FILE = '/home/ubuntu/.picoclaw/logs/apex-autopilot.json'


def _is_practice_mode() -> bool:
    """
    Returns True if apex-autopilot.json mode is PRACTICE (or file absent/unreadable).
    The mode field in apex-autopilot.json is authoritative:
      "mode": "PRACTICE"  → dry-run enforced regardless of CLI flags
      "mode": "LIVE"      → real execution proceeds (user must set this explicitly)
    Safe default: PRACTICE.
    """
    try:
        with open(_AUTOPILOT_FILE) as f:
            config = json.load(f)
        return config.get('mode', 'PRACTICE') != 'LIVE'
    except Exception:
        return True  # Safe default — never accidentally go live


def _get_mode() -> str:
    """Return mode string from autopilot config ('LIVE' or 'PRACTICE')."""
    try:
        with open(_AUTOPILOT_FILE) as f:
            return json.load(f).get('mode', 'PRACTICE')
    except Exception:
        return 'PRACTICE'


# Maximum favourable/unfavourable drift from signal price before rejection.
# Tighter than queue-revalidate thresholds because this fires at execution time,
# not at morning revalidation. A 1% drift between scan and execution means EV
# is already off by significant basis points, and slippage is compounded.
# For LONG entries:
#   drift_pct > 0 = price went UP  (bad for TREND — chasing)
#   drift_pct < 0 = price went DOWN (bad for INVERSE — already moved)
_STALENESS_LIMITS = {
    # signal_type: (max_up_drift_pct, max_down_drift_pct)
    'TREND':            (1.0,  3.0),  # Up 1% = chasing; down 3% = broken
    'EARNINGS_DRIFT':   (1.0,  3.0),
    'DIVIDEND_CAPTURE': (1.5,  2.5),
    'CONTRARIAN':       (3.0,  5.0),  # Contrarian tolerates wider drift
    'TACO_CONTRARIAN':  (3.0,  5.0),
    'INVERSE':          (2.0,  2.0),  # Inverse ETF — tight on either side
    'GEO_REVERSAL':     (3.0,  3.0),
    'DEFAULT':          (1.5,  3.0),
}


def _check_entry_staleness(ticker: str, signal_entry: float, signal_type: str) -> dict:
    """
    Verify the signal's entry price is still close to the current market price.
    Returns {'ok': bool, 'current': float, 'drift_pct': float, 'reason': str}.

    Fail-open: if price fetch fails, allow the trade (don't block on data
    failures — queue revalidation already did the morning check).
    """
    try:
        import yfinance as yf
        # Convert T212 ticker to yahoo equivalent via existing map
        from apex_utils import safe_read
        ticker_map = safe_read('/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json', {}) or {}
        # apex-ticker-map.json stores {yahoo_key: {t212: ..., currency: ...}}
        # Build reverse map: t212 ticker → yahoo key
        reverse_map = {}
        for yahoo_key, entry in ticker_map.items():
            if isinstance(entry, dict) and entry.get('t212'):
                reverse_map[entry['t212']] = yahoo_key
        yahoo_ticker = reverse_map.get(ticker)
        if yahoo_ticker:
            # Append .L for ANY LSE-listed T212 instrument, regardless of the
            # ticker-map currency tag. T212 LSE suffixes are 'l_EQ', 'm_EQ',
            # 's_EQ', 'd_EQ' — all surface as <KEY>.L on Yahoo (Yahoo returns
            # the LSE listing's quote currency automatically). Without this,
            # HEALm_EQ resolved to bare 'HEAL' which is the unrelated US REIT
            # @ $25.86 → false +185% drift abort on 2026-04-16.
            _is_lse = (
                ticker.endswith('l_EQ') or
                ticker.endswith('m_EQ') or
                ticker.endswith('s_EQ') or
                ticker.endswith('d_EQ')
            )
            currency = ticker_map.get(yahoo_ticker, {}).get('currency', 'USD')
            if _is_lse or currency in ('GBX', 'GBP'):
                yahoo_ticker = f"{yahoo_ticker}.L"
        else:
            # Best-effort: strip T212 suffix (order matters for 'l_EQ' LSE tickers)
            yahoo_ticker = ticker.replace('_US_EQ', '').replace('l_EQ', '').replace('_EQ', '')

        hist = yf.Ticker(yahoo_ticker).history(period='1d', interval='5m')
        if hist.empty:
            return {'ok': True, 'current': None, 'drift_pct': 0.0,
                    'reason': 'no live price available — allowing'}
        current = float(hist['Close'].iloc[-1])

        # Handle LSE pence quotes on UK instruments
        if yahoo_ticker.endswith('.L') and current > signal_entry * 10:
            current = round(current / 100, 4)

        if signal_entry <= 0:
            return {'ok': True, 'current': current, 'drift_pct': 0.0, 'reason': 'no signal entry to compare'}

        drift_pct = (current - signal_entry) / signal_entry * 100

        max_up, max_down = _STALENESS_LIMITS.get(signal_type, _STALENESS_LIMITS['DEFAULT'])

        if drift_pct > max_up:
            return {
                'ok':        False,
                'current':   round(current, 4),
                'drift_pct': round(drift_pct, 2),
                'reason':    (f"Price drifted UP {drift_pct:+.2f}% "
                              f"(limit +{max_up}% for {signal_type}) — chasing, entry invalidated")
            }
        if drift_pct < -max_down:
            return {
                'ok':        False,
                'current':   round(current, 4),
                'drift_pct': round(drift_pct, 2),
                'reason':    (f"Price drifted DOWN {drift_pct:+.2f}% "
                              f"(limit -{max_down}% for {signal_type}) — thesis broken, entry invalidated")
            }
        return {
            'ok':        True,
            'current':   round(current, 4),
            'drift_pct': round(drift_pct, 2),
            'reason':    'within tolerance'
        }
    except Exception as e:
        # Fail-closed: price feed errors block execution.
        # A stale price assumption invalidates EV and R-multiple calculations.
        # The morning queue-revalidation already passed — if the live feed is
        # now down, do not commit capital on an unverified entry.
        return {'ok': False, 'current': None, 'drift_pct': 0.0,
                'reason': f'staleness check failed (price feed down): {e} — blocking until feed recovers'}


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    line = f"{ts}: {msg}"
    print(line)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


_PRICE_FIELDS = ('entry', 'stop', 'target1', 'target2', 'trailing_stop_level')


def _update_position(ticker: str, updates: dict) -> None:
    """Apply *updates* to the position matching *ticker*, under file lock.
    All price fields are rounded to 4 decimal places before writing to prevent
    floating-point residue (e.g. 7.186822400523831 from FX/ATR math).
    """
    # Round price fields in the update dict itself
    _clean = {}
    for k, v in updates.items():
        if k in _PRICE_FIELDS and v is not None:
            try:
                _clean[k] = round(float(v), 4)
            except (TypeError, ValueError):
                _clean[k] = v
        else:
            _clean[k] = v

    def _apply(positions):
        positions = positions or []
        for p in positions:
            if p.get('t212_ticker') == ticker:
                p.update(_clean)
                return positions
        return positions
    locked_read_modify_write(POSITIONS_FILE, _apply, default=[])


def _remove_pending(ticker: str) -> None:
    """Remove a pending, entry_placed, or awaiting_fill entry that never confirmed.
    Covers three cases:
    - status='pending'        — order was never even placed (pre-placement abort)
    - status='entry_placed'   — limit order was placed but cancelled unfilled during
                                market hours; executor upgrades pending→entry_placed
                                before the fill check, so both statuses must be cleaned
                                up here to prevent a ghost entry_placed record that
                                confuses reconcile and triggers stale watchdog alerts.
    - status='awaiting_fill'  — executor crashed after upgrading entry_placed→awaiting_fill
                                but before confirming the fill; without cleanup a re-run
                                creates a duplicate position.
    """
    def _rm(positions):
        return [p for p in (positions or [])
                if not (p.get('t212_ticker') == ticker
                        and p.get('status') in ('pending', 'entry_placed', 'awaiting_fill'))]
    locked_read_modify_write(POSITIONS_FILE, _rm, default=[])


def execute(signal: dict, dry_run: bool = False, _mode: str = None) -> bool:
    """
    Execute a trade from a signal dict.
    Returns True on full success (entry + stop placed).
    _mode: pre-read mode string — callers should pass this to avoid a
    re-read race where mode could change between signal generation and execution.
    """
    # ── Mode locked at call time — do not re-read mid-execution ──────────────
    # If mode flips PRACTICE → LIVE between signal generation and execution,
    # the executor uses whichever mode was captured at main() entry.
    if _mode is None:
        _mode = _get_mode()

    ticker   = signal.get('t212_ticker', '')
    name     = signal.get('name', ticker)
    quantity = float(signal.get('quantity', 0) or 0)
    entry    = float(signal.get('entry', 0) or 0)
    stop     = float(signal.get('stop', 0) or 0)
    target1  = float(signal.get('target1', 0) or 0)
    target2  = float(signal.get('target2', 0) or 0)
    score    = float(signal.get('score', 0) or 0)
    rsi      = float(signal.get('rsi', 0) or 0)
    macd     = float(signal.get('macd', 0) or 0)
    sector   = signal.get('sector') or 'UNKNOWN'
    atr      = signal.get('atr', 0)
    signal_type = signal.get('signal_type', 'TREND')
    currency = signal.get('currency', 'GBP')

    # Round quantity to T212's required precision for this instrument.
    # Some instruments require whole numbers (quantityPrecision=0), others
    # allow up to 2 dp. Sending the wrong precision causes HTTP 400
    # "/api-errors/quantity-precision-mismatch".
    if ticker:
        original_qty = quantity
        quantity = _round_quantity(quantity, ticker)
        if quantity != original_qty:
            _log(f"Quantity adjusted for T212 precision: {original_qty} → {quantity} ({ticker})")

    if not ticker or not quantity:
        _log(f"ERROR: Signal missing ticker or quantity — aborting")
        send_telegram("⚠️ Signal file incomplete — no ticker or quantity.")
        try:
            os.remove(PROCESSING_FILE)
        except FileNotFoundError:
            pass
        _remove_pending(name)
        return False

    # ── Ticker-map currency reconciliation + FX pre-validation ──────────────
    # The signal's `currency` field comes from apex-market-data.py's WATCHLIST,
    # which has historically disagreed with the actual T212 trading currency
    # (e.g. WATCHLIST said HEAL=EUR but yfinance HEAL.L returns USD; WATCHLIST
    # said IUCD=GBP but T212 trades IUCDl_EQ in USD; WATCHLIST said VAPX=CHF
    # but yfinance VAPX.L returns GBP). Trust the ticker-map for both sides:
    #   • `currency`        = T212 trading currency (ground truth for API)
    #   • `yahoo_currency`  = yfinance quote currency (signal-source unit)
    #
    # FX strategy — defer the actual conversion until just before API submission:
    #   1. Override `currency` to T212 truth NOW (downstream gates use it)
    #   2. Validate the FX conversion can be performed (fail-CLOSED if not)
    #   3. Store _yahoo_currency + _needs_fx flag for the limit-price step
    #   4. Leave entry/stop/target1/target2 in their original yfinance unit
    #
    # The staleness check (further down) compares the yfinance live price
    # against `entry`. Both must be in the same unit (yfinance currency), so
    # we MUST NOT convert entry until after the staleness check. The actual
    # FX conversion happens at the limit-price construction below, alongside
    # the existing GBX pence×100 branch.
    #
    # See 2026-04-16 — Phase 2 FX layer (replaces the earlier hard-block guard).
    _yahoo_currency = None
    _needs_fx = False

    def _norm_for_fx(c):
        return 'GBP' if (c or '').upper() in ('GBX', 'GBPENCE') else (c or '').upper()

    try:
        _tmap = safe_read('/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json', {}) or {}
        _t212_currency = None
        for _yk, _ent in _tmap.items():
            if isinstance(_ent, dict) and _ent.get('t212') == ticker:
                _t212_currency = (_ent.get('currency') or '').upper()
                _yahoo_currency = (_ent.get('yahoo_currency') or '').upper() or None
                break

        # Override the signal's claimed currency with the T212 truth so all
        # downstream branches (market-hours gate, GBX pence conversion, FX
        # snapshot, outcomes log) use the correct value. Safe for both LSE
        # and US tickers.
        if _t212_currency and _t212_currency != currency:
            _log(f"Currency override: signal said {currency}, T212 says {_t212_currency} — using T212 value")
            currency = _t212_currency

        # FX pre-validation: signal prices are quoted in `_yahoo_currency`
        # but T212 expects `_t212_currency`. Skip when:
        #   • ticker-map has no yahoo_currency entry (treat as already aligned)
        #   • the two currencies are the same
        #   • GBX vs GBP — handled by the pence×100 branch downstream
        if (_yahoo_currency and _t212_currency
                and _norm_for_fx(_yahoo_currency) != _norm_for_fx(_t212_currency)):
            _needs_fx = True
            try:
                # Probe the rate now — fail fast if FX feed is unavailable.
                _probe = convert_price(entry, _yahoo_currency, _t212_currency)
                _log(
                    f"FX needed {_yahoo_currency}→{_t212_currency} for {ticker} — "
                    f"probe entry {entry:.4f}→{_probe:.4f} (will apply at API call)"
                )
            except FxRateUnavailable as _fx_err:
                _log(
                    f"FX RATE UNAVAILABLE: cannot convert {_yahoo_currency}→{_t212_currency} "
                    f"for {ticker} — blocking. Reason: {_fx_err}"
                )
                send_telegram(
                    f"🛑 FX RATE UNAVAILABLE\n\n"
                    f"{name} ({ticker})\n"
                    f"Need {_yahoo_currency}→{_t212_currency} conversion but yfinance "
                    f"fetch failed and no fresh cache entry. Trade declined to avoid "
                    f"a wrongly-priced limit.\n\n"
                    f"Reason: {_fx_err}\n"
                    f"Will retry next cycle when FX feed recovers."
                )
                try:
                    os.remove(PROCESSING_FILE)
                except FileNotFoundError:
                    pass
                _remove_pending(name)
                return False
    except Exception as _ce:
        _log(f"WARN: ticker-map currency lookup failed for {ticker}: {_ce} — using signal value {currency}")

    # ── NaN/Inf pre-flight gate ───────────────────────────────────────────────
    # Python's json module serialises float('nan') as the bare word NaN which
    # is invalid JSON.  If a NaN leaks through (e.g. from a failed price fetch
    # or calculation error), it must be caught before any API call is made.
    _nan_fields = []
    for _fname, _fval in [('quantity', quantity), ('entry', entry), ('stop', stop)]:
        if math.isnan(_fval) or math.isinf(_fval) or _fval <= 0:
            _nan_fields.append(f"{_fname}={_fval}")
    if _nan_fields:
        _log(f"ERROR: Signal has NaN/zero critical fields: {', '.join(_nan_fields)} — aborting and removing signal")
        send_telegram(
            f"⚠️ SIGNAL REJECTED — NaN/INVALID FIELDS\n\n"
            f"{name} ({ticker})\n"
            f"Bad fields: {', '.join(_nan_fields)}\n\n"
            f"Signal deleted. Re-run morning scan to regenerate."
        )
        try:
            os.remove(PROCESSING_FILE)
        except FileNotFoundError:
            pass
        return False

    # ── ATR sanity check — rebuild degenerate stops/targets if ATR is zero ──────
    # ATR=0 means the yfinance fetch failed or returned insufficient history.
    # The signal's stop/target values were calculated from ATR and will be
    # degenerate (stop = entry, target = entry).  Use fixed-percentage fallbacks
    # rather than rejecting the trade entirely.
    _ATR_FALLBACK_PCT = {
        'TREND':            0.06,   # 6% stop, 9% / 15% targets
        'CONTRARIAN':       0.04,   # 4% stop (tighter — buying into weakness)
        'EARNINGS_DRIFT':   0.05,
        'DIVIDEND_CAPTURE': 0.03,
        'INVERSE':          0.04,
    }
    try:
        _atr_val = float(atr) if atr else 0.0
        if _atr_val <= 0 and entry > 0:
            _pct = _ATR_FALLBACK_PCT.get(signal_type, 0.05)
            _stop_fallback    = round(entry * (1 - _pct), 4)
            _target1_fallback = round(entry * (1 + _pct * 1.5), 4)
            _target2_fallback = round(entry * (1 + _pct * 2.5), 4)
            # Only override if current values are degenerate (equal to entry or zero)
            if stop <= 0 or abs(stop - entry) / entry < 0.002:
                stop    = _stop_fallback
                log_warning(f"{name} ATR=0: stop rebuilt as {_pct*100:.0f}% fallback → {stop}")
            if target1 <= 0 or abs(target1 - entry) / entry < 0.002:
                target1 = _target1_fallback
            if target2 <= 0 or abs(target2 - entry) / entry < 0.002:
                target2 = _target2_fallback
    except (TypeError, ValueError):
        log_warning(f"Signal for {name} has non-numeric ATR={atr!r}")

    if entry > 0 and stop > 0 and stop >= entry:
        _log(f"ERROR: Invalid stops for {name}: entry={entry} stop={stop} — stop must be below entry")
        send_telegram(f"⚠️ Trade rejected — invalid stops: entry {entry} <= stop {stop} for {name}")
        return False

    # Practice mode gate — mode was captured at main() entry and is now fixed
    if dry_run or _mode != 'LIVE':
        _log(f"DRY-RUN [{_mode}]: Would place {quantity} × {ticker} @ £{entry} (stop £{stop})")
        send_telegram(f"🔬 DRY-RUN [{_mode}]: {name} ({ticker}) {quantity}×£{entry} stop:£{stop}")
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Alpaca routing: US stocks with Alpaca credentials configured go via Alpaca
    # (DMA, smart order routing, fractional shares) — T212 is fallback.
    # ─────────────────────────────────────────────────────────────────────────
    alpaca_ticker = signal.get('ticker', ticker).replace('_US_EQ', '').replace('_EQ', '')
    use_alpaca = (
        _ALPACA_AVAILABLE
        and _alpaca_mod is not None
        and alpaca_ticker.upper() in _ALPACA_US_TICKERS
    )

    if use_alpaca:
        _log(f"Routing {alpaca_ticker} → Alpaca (US stock, DMA available)")
        alpaca_signal = {**signal, 'ticker': alpaca_ticker}
        ap_result = _alpaca_mod.execute(alpaca_signal, dry_run=dry_run)

        if ap_result['success']:
            entry_id   = ap_result.get('entry_order_id')
            stop_id    = ap_result.get('stop_order_id')
            filled_qty = ap_result.get('filled_qty', quantity)
            venue      = 'ALPACA'

            # Write position to positions file (same schema as T212 path)
            today    = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            now_iso  = datetime.now(timezone.utc).isoformat()
            status   = 'protected' if stop_id else ('awaiting_fill' if filled_qty == 0 else 'unprotected')
            unprotected = not stop_id and filled_qty > 0

            def _write_alpaca(positions):
                positions = positions or []
                positions = [p for p in positions
                             if not (p.get('t212_ticker') == ticker and p.get('status') == 'pending')]
                positions.append({
                    "t212_ticker": ticker, "name": name,
                    "quantity": quantity, "entry": entry, "stop": stop,
                    "target1": target1, "target2": target2, "score": score,
                    "rsi": rsi, "macd": macd, "sector": sector, "atr": atr,
                    "signal_type": signal_type, "currency": currency,
                    "fx_at_entry": get_fx_rate(currency),
                    "opened": today, "opened_iso": now_iso,
                    "entry_order_id": str(entry_id) if entry_id else None,
                    "stop_order_id": str(stop_id) if stop_id else None,
                    "status": status, "order_type": f"{ap_result.get('order_type','LIMIT')}+STOP",
                    "venue": "ALPACA", "unprotected": unprotected,
                    "unrealised_pnl": 0.0,
                    "regime_at_entry": safe_read(REGIME_FILE, {}).get('overall', 'UNKNOWN'),
                })
                return positions
            locked_read_modify_write(POSITIONS_FILE, _write_alpaca, default=[])

            if unprotected:
                send_telegram(
                    f"🚨 UNPROTECTED POSITION (Alpaca) — ACTION REQUIRED\n\n"
                    f"{name} ({alpaca_ticker})\nEntry placed but stop loss FAILED.\n"
                    f"Log in to Alpaca and set stop at ${stop}"
                )
            elif status == 'protected':
                send_telegram(
                    f"✅ TRADE PLACED (Alpaca)\n"
                    f"🏷 {name} ({alpaca_ticker})\n"
                    f"📐 Qty: {filled_qty} shares\n"
                    f"💰 Entry: ${entry} ({ap_result.get('order_type','LIMIT')})\n"
                    f"🛑 Stop: ${stop} (GTC)\n"
                    f"🎯 T1: ${target1} | T2: ${target2}\n"
                    f"📊 Score: {score}/10\n"
                    f"🔖 Entry ID: {entry_id} | Stop ID: {stop_id}\n"
                    f"🏦 Venue: Alpaca (DMA)"
                )
                try:
                    os.remove(PROCESSING_FILE)
                except FileNotFoundError:
                    pass
            return True
        else:
            _log(f"Alpaca execution failed: {ap_result.get('error')} — falling back to T212")

    # ─────────────────────────────────────────────────────────────────────────
    # Pre-flight: Re-validate portfolio heat at execution time.
    # The heat gate already ran during signal evaluation (apex-autopilot.py
    # safety_check), but new positions may have been added since then.
    # This ensures we never exceed the 8% heat cap at the moment of order
    # placement, not just at signal generation time.
    # Non-blocking on error — never let monitoring failure abort execution.
    # ─────────────────────────────────────────────────────────────────────────
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "ph", "/home/ubuntu/.picoclaw/scripts/apex-portfolio-heat.py")
        _ph = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_ph)
        _heat_mult, _heat_status, _heat_pct = _ph.get_heat_multiplier()
        if _heat_status == 'CRITICAL':
            _log(f"EXECUTION ABORTED: portfolio heat {_heat_pct:.1f}% exceeds 8% cap at order time")
            send_telegram(
                f"🛑 ORDER ABORTED — portfolio heat\n\n"
                f"{name} ({ticker})\n"
                f"Heat at execution time: {_heat_pct:.1f}% (max 8%).\n"
                f"Signal preserved — will retry when risk reduces."
            )
            return False
    except Exception as _heat_err:
        log_warning(f"Portfolio heat pre-flight check failed (non-blocking): {_heat_err}")

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    now_iso = datetime.now(timezone.utc).isoformat()

    # ─────────────────────────────────────────────────────────────────────────
    # Staleness gate: reject if price has drifted unfavourably since scan.
    # The signal was generated at scan time (e.g. 08:30 UTC) but may not be
    # executing until 09:30 or 13:05 UTC. Price can drift significantly in
    # that window, invalidating the R-multiple and EV calculation.
    # Thresholds per signal type are tighter here than in queue-revalidate
    # because this is the last check before committing capital.
    # ─────────────────────────────────────────────────────────────────────────
    stale_check = _check_entry_staleness(ticker, entry, signal_type)
    if not stale_check['ok']:
        _log(f"STALENESS ABORT: {stale_check['reason']}")
        feed_down = stale_check.get('current') is None  # True = feed error, False = actual drift
        if feed_down:
            # Price feed is unavailable — preserve the signal and retry next cycle.
            # Deleting here would silently kill a valid signal on a temporary outage.
            send_telegram(
                f"⏱ ENTRY DEFERRED — PRICE FEED DOWN\n\n"
                f"{name} ({ticker})\n"
                f"Signal entry: £{entry}\n\n"
                f"{stale_check['reason']}\n"
                f"Signal preserved — will retry when feed recovers."
            )
        else:
            # Price has genuinely drifted — thesis is invalidated, delete signal.
            send_telegram(
                f"⏱ ENTRY REJECTED — PRICE DRIFT\n\n"
                f"{name} ({ticker})\n"
                f"Signal entry: £{entry}\n"
                f"Current price: £{stale_check['current']}\n"
                f"Drift: {stale_check['drift_pct']:+.2f}%\n\n"
                f"{stale_check['reason']}\n"
                f"No position opened — waiting for better entry."
            )
            try:
                os.remove(PROCESSING_FILE)
            except FileNotFoundError:
                pass
        return False

    if stale_check.get('current'):
        _log(f"Staleness OK: signal £{entry} vs current £{stale_check['current']} "
             f"({stale_check['drift_pct']:+.2f}%)")

    # ─────────────────────────────────────────────────────────────────────────
    # FX conversion: yfinance currency → T212 trading currency
    #
    # Run AFTER the staleness check (which uses yfinance live price against
    # the original yfinance-currency entry) and BEFORE the pending write so
    # that positions.json, the broker watchdog, the trailing stop, and every
    # downstream consumer all see consistent T212-currency values.
    #
    # GBX vs GBP is treated as no-FX (handled separately by pence×100 in the
    # limit-price section below). Pre-flight validation already happened at
    # the top of execute(), so a FxRateUnavailable here means the cache went
    # stale between the probe and now — fail-CLOSED.
    # ─────────────────────────────────────────────────────────────────────────
    if _needs_fx and _yahoo_currency:
        try:
            _orig_entry, _orig_stop = entry, stop
            entry   = convert_price(entry,   _yahoo_currency, currency)
            stop    = convert_price(stop,    _yahoo_currency, currency)
            target1 = convert_price(target1, _yahoo_currency, currency) if target1 > 0 else target1
            target2 = convert_price(target2, _yahoo_currency, currency) if target2 > 0 else target2
            _log(
                f"FX applied {_yahoo_currency}→{currency}: "
                f"entry {_orig_entry:.4f}→{entry:.4f}  stop {_orig_stop:.4f}→{stop:.4f}  "
                f"T1→{target1:.4f}  T2→{target2:.4f}"
            )
        except FxRateUnavailable as _fx_err:
            _log(f"FX RATE LOST mid-execution for {ticker}: {_fx_err} — aborting")
            send_telegram(
                f"🛑 FX RATE LOST MID-EXECUTION\n\n"
                f"{name} ({ticker})\n"
                f"Pre-flight FX probe succeeded but conversion failed at order "
                f"placement. No order placed.\n\nReason: {_fx_err}"
            )
            _remove_pending(ticker)
            try:
                os.remove(PROCESSING_FILE)
            except FileNotFoundError:
                pass
            return False

        # Sanity: stop must remain strictly below entry post-FX. If they cross
        # (e.g. signal generated stop very close to entry, FX rates jittered)
        # the order would be self-triggering — refuse.
        if stop >= entry:
            _log(f"POST-FX SANITY: stop {stop:.4f} >= entry {entry:.4f} after "
                 f"{_yahoo_currency}→{currency} conversion — aborting (would self-trigger)")
            send_telegram(
                f"🛑 POST-FX SANITY ABORT\n\n"
                f"{name} ({ticker})\n"
                f"After FX conversion the stop ({stop:.4f}) is at or above the "
                f"entry ({entry:.4f}) — order would self-trigger. No trade placed."
            )
            _remove_pending(ticker)
            try:
                os.remove(PROCESSING_FILE)
            except FileNotFoundError:
                pass
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Step 0: Write PENDING entry BEFORE any API call
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 0: Writing PENDING entry for {ticker}")

    try:
        fx_at_entry = get_fx_rate(currency)
    except Exception as _fx_e:
        if currency and currency.upper() != "GBP":
            log_error(f"FX rate fetch failed for {currency}: {_fx_e} — trade blocked")
            send_telegram(
                f"⚠️ FX rate unavailable for {ticker} — trade blocked to prevent mis-sizing"
            )
            return False
        log_warning(f"FX snapshot failed for {currency}: {_fx_e}")
        fx_at_entry = 1.0

    def _write_pending(positions):
        positions = positions or []
        # Remove stale pending for same ticker (safety dedup)
        positions = [p for p in positions
                     if not (p.get('t212_ticker') == ticker
                             and p.get('status') == 'pending')]
        positions.append({
            "t212_ticker":    ticker,
            "name":           name,
            "quantity":       quantity,
            "entry":          entry,
            "stop":           stop,
            "target1":        target1,
            "target2":        target2,
            "score":          score,
            "rsi":            rsi,
            "macd":           macd,
            "sector":         sector,
            "atr":            atr,
            "signal_type":    signal_type,
            "currency":       currency,
            "fx_at_entry":    fx_at_entry,
            "opened":         today,
            "opened_iso":     now_iso,
            "entry_order_id": None,
            "stop_order_id":  None,
            "status":         "pending",
            "order_type":     "LIMIT+STOP",
            "unrealised_pnl": 0.0,
            "regime_at_open": _regime_snapshot(),
            "regime_at_entry": safe_read(REGIME_FILE, {}).get('overall', 'UNKNOWN'),
        })
        return positions

    locked_read_modify_write(POSITIONS_FILE, _write_pending, default=[])

    # ── Market hours gate — never place orders for closed exchanges ───────────
    try:
        _mh_cal = safe_read('/home/ubuntu/.picoclaw/logs/apex-market-calendar.json', {})
        _mh_today = _mh_cal.get('today', {})
        _mh_us_open = _mh_today.get('us_currently_open', True)
        _mh_uk_open = _mh_today.get('uk_currently_open', True)
        _mh_blocked = (
            (currency == 'USD' and not _mh_us_open) or
            (currency in ('GBX', 'GBP') and not _mh_uk_open) or
            (currency in ('EUR', 'CHF') and not _mh_uk_open)
        )
        if _mh_blocked:
            _log(f"Market closed for {currency} instrument — aborting execution. "
                 f"US open: {_mh_us_open}, UK open: {_mh_uk_open}")
            _remove_pending(ticker)
            return False
    except Exception:
        pass  # fail-open — if calendar unreadable, proceed

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Place limit entry order (fallback to market)
    # GBX instruments (LSE stocks quoted in pence) require the T212 API price
    # in pence, not pounds.  Signal prices are always stored in pounds, so
    # multiply by 100 before sending to T212 for GBX instruments.
    #
    # Slippage premium: a passive BUY limit at the inside ask never crosses
    # the spread for illiquid instruments (VAGS bond ETF sat NEW for 9 polls
    # × 20 s without a fill on 2026-04-16). Apply a small premium to make it
    # a "marketable limit" — still capped (no runaway market fill) but priced
    # through the inside ask. Hard-capped at half the entry-to-stop distance
    # so we never enter already inside the stop's risk envelope.
    # ─────────────────────────────────────────────────────────────────────────
    # VIX-scaled premium: higher VIX → wider spreads → need larger premium to cross.
    # Formula: base_bps × (1 + max(0, (vix-18)/20))
    #   VIX=18 → 1.0× (no adjustment)   VIX=28 → 1.5×   VIX=38 → 2.0×
    # Illiquid instruments use a higher base and also scale with VIX.
    _base_bps = (T212_LIMIT_PREMIUM_BPS_ILLIQUID
                 if ticker in T212_ILLIQUID_TICKERS
                 else T212_LIMIT_PREMIUM_BPS)
    try:
        import json as _json
        _mdir = _json.loads(open(f'{_LOGS}/apex-market-direction.json').read())
        _vix_now = float(_mdir.get('vix', _mdir.get('vix_current', 18)) or 18)
    except Exception:
        _vix_now = 18.0
    _vix_scale   = 1.0 + max(0.0, (_vix_now - 18.0) / 20.0)
    _premium_bps  = round(_base_bps * _vix_scale, 1)
    _premium_frac = _premium_bps / 10000.0
    # Safety cap: never exceed half the entry-to-stop distance
    if entry > 0 and stop > 0 and entry > stop:
        _max_safe_frac = ((entry - stop) / entry) * T212_LIMIT_PREMIUM_MAX_FRAC_OF_STOP
        if _premium_frac > _max_safe_frac:
            _log(f"Premium capped: {_premium_bps} bps would exceed half stop-distance "
                 f"(entry £{entry}, stop £{stop}) — using {_max_safe_frac*10000:.1f} bps")
            _premium_frac = max(0.0, _max_safe_frac)
    _entry_with_premium = entry * (1.0 + _premium_frac)
    _premium_applied_bps = _premium_frac * 10000.0
    if _premium_applied_bps > 0:
        _log(f"Limit premium: +{_premium_applied_bps:.1f} bps "
             f"({'illiquid' if ticker in T212_ILLIQUID_TICKERS else 'std'} "
             f"× VIX{_vix_now:.0f}={_vix_scale:.2f}x) "
             f"→ {entry:.4f} → {_entry_with_premium:.4f}")

    # FX conversion (if any) was already applied above before the pending
    # write, so `entry`, `stop` are already in T212 trading currency. The
    # only remaining currency transform is GBX pence×100 for LSE GBX-quoted
    # instruments (CHF/EUR/USD instruments skip the pence step).
    t212_entry_price = (round(_entry_with_premium * 100, 2) if currency == 'GBX'
                        else round(_entry_with_premium, 4))
    t212_stop_price  = (round(stop * 100, 2) if currency == 'GBX'
                        else round(stop,  4))
    _log(f"Step 1: Placing LIMIT entry — {ticker} ×{quantity} @ "
         f"{'%dp' % t212_entry_price if currency == 'GBX' else '%.4f' % _entry_with_premium} "
         f"(currency={currency})")

    entry_data = t212_request('/equity/orders/limit', method='POST', payload={
        "ticker":       ticker,
        "quantity":     quantity,
        "limitPrice":   t212_entry_price,
        "timeValidity": "DAY",
    })

    entry_id = (entry_data or {}).get('id')
    order_type_used = "LIMIT"

    if not entry_id:
        _log(f"Step 1b: Limit failed — falling back to market order")
        market_data = t212_request('/equity/orders/market', method='POST', payload={
            "ticker":   ticker,
            "quantity": quantity,
        })
        entry_id = (market_data or {}).get('id')
        order_type_used = "MARKET"

    if not entry_id:
        _log(f"FATAL: Entry order failed for {ticker}")
        _remove_pending(ticker)
        send_telegram(
            f"❌ ENTRY ORDER FAILED\n\n"
            f"{name} ({ticker})\n"
            f"Both limit and market orders failed.\n"
            f"Pending entry removed — no position opened."
        )
        return False

    _log(f"Step 1 OK: Entry ID {entry_id} ({order_type_used})")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Upgrade pending → entry_placed
    # ─────────────────────────────────────────────────────────────────────────
    _update_position(ticker, {
        'status':         'entry_placed',
        'entry_order_id': str(entry_id),
        'order_type':     f'{order_type_used}+STOP',
    })
    _audit(signal_id=None, ticker=ticker, signal_type=signal_type,
           from_state='pending', to_state='entry_placed',
           detail=f'{order_type_used} entry order placed', t212_order_id=entry_id)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2b: Wait for entry fill before placing stop
    # T212 rejects stop orders for shares not yet owned (e.g. limit placed
    # pre-market won't fill until exchange opens).  Poll up to 3 minutes;
    # if still unfilled, save state and let fill-check.sh finish later.
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 2b: Waiting for entry fill — polling order {entry_id}")
    filled_qty = 0.0
    for _poll in range(T212_FILL_POLL_COUNT):
        _raw = t212_request(f'/equity/orders/{entry_id}')
        if _raw is None:
            # API returned None — 404 (order gone) or transient error.
            # Don't keep hammering a dead order ID every 10s for 3 minutes.
            _log(f"  Poll {_poll+1}: order {entry_id} not found (API error/404) — deferring stop")
            break
        order_status = _raw
        filled_qty = float(order_status.get('filledQuantity', 0))
        status_str = order_status.get('status', 'UNKNOWN')
        if filled_qty > 0:
            _log(f"  Entry filled: {filled_qty} shares (status: {status_str})")
            break
        if status_str in ('CANCELLED', 'REJECTED', 'EXPIRED'):
            _log(f"  Entry order {status_str} — aborting stop placement")
            _remove_pending(ticker)
            send_telegram(
                f"⚠️ ENTRY ORDER {status_str}\n\n{name} ({ticker})\n"
                f"Order {entry_id} was {status_str}. No position opened."
            )
            return False
        _log(f"  Poll {_poll+1}/{T212_FILL_POLL_COUNT}: filledQty={filled_qty} "
             f"status={status_str} — waiting {T212_FILL_POLL_INTERVAL}s")
        time.sleep(T212_FILL_POLL_INTERVAL)

    if filled_qty == 0:
        # During market hours a limit that hasn't filled in 3 min is unlikely
        # to fill before session end — cancel it explicitly to prevent a phantom
        # awaiting_fill record persisting until the next watchdog cycle.
        # Pre-market entries are left open (expected DAY behaviour).
        _now_utc   = datetime.now(timezone.utc)
        _hour_min  = _now_utc.hour * 60 + _now_utc.minute
        _intraday  = 480 <= _hour_min <= 930 and _now_utc.weekday() < 5  # 08:00–15:30 Mon–Fri
        if _intraday:
            _log(f"Intraday non-fill — cancelling limit order {entry_id} to prevent phantom position")
            try:
                t212_request(f'/equity/orders/{entry_id}', method='DELETE')
            except Exception as _ce:
                log_warning(f"Could not cancel unfilled limit order {entry_id}: {_ce}")

            # ── Late-fill guard ───────────────────────────────────────────────
            # A fill can arrive in the last milliseconds of the poll window and
            # be processed by T212 after our DELETE.  Check the portfolio once
            # before removing the pending record — if the position already exists
            # in T212 we must NOT remove it or we create a dark, unprotected
            # position.  This is what happened with ULVR on 2026-04-13.
            try:
                _portfolio = t212_request('/equity/portfolio') or []
                _late_fill = next(
                    (p for p in _portfolio if p.get('ticker') == ticker), None
                )
            except Exception as _pfe:
                log_warning(f"Portfolio check after cancel failed for {ticker}: {_pfe}")
                _late_fill = None

            if _late_fill:
                _filled_qty = float(_late_fill.get('quantity', quantity))
                _avg_price  = float(_late_fill.get('averagePrice', entry))
                # Convert T212 price back to pounds for GBX instruments
                if currency == 'GBX' and _avg_price > entry * 10:
                    _avg_price = round(_avg_price / 100, 4)
                log_warning(
                    f"Late fill detected for {name} ({ticker}): "
                    f"{_filled_qty} shares @ {_avg_price} — order filled after poll window. "
                    f"Recovering position record and placing stop."
                )
                # Update pending record with correct fill price and quantity
                _update_position(ticker, {
                    'entry':    _avg_price,
                    'quantity': _filled_qty,
                })
                # Fall through to stop placement below (filled_qty set)
                filled_qty = _filled_qty
                send_telegram(
                    f"⚠️ LATE FILL DETECTED — RECOVERING\n\n"
                    f"{name} ({ticker})\n"
                    f"Filled {_filled_qty} shares @ £{_avg_price} after poll window.\n"
                    f"Placing stop automatically..."
                )
            else:
                _audit(signal_id=None, ticker=ticker, signal_type=signal_type,
                       from_state='entry_placed', to_state='REMOVED',
                       detail='intraday non-fill: limit cancelled, no late fill detected',
                       t212_order_id=entry_id)
                _remove_pending(ticker)
                send_telegram(
                    f"⚠️ ENTRY LIMIT NOT FILLED (CANCELLED)\n\n"
                    f"{name} ({ticker})\n"
                    f"Limit order @ £{entry} did not fill within 3 min during market hours.\n"
                    f"Order cancelled. No position opened — signal will re-qualify on next scan."
                )
                return False

        # Pre-market or low-liquidity — defer stop to fill-check watchdog.
        _log(f"Entry not filled within 3 min — deferring stop to fill-check")
        _update_position(ticker, {
            'status':         'awaiting_fill',
            'entry_order_id': str(entry_id),
            'stop_price':     stop,
            'deferred_stop':  True,
        })
        send_telegram(
            f"⏳ ENTRY PENDING — STOP DEFERRED\n\n"
            f"{name} ({ticker})\n"
            f"Limit order {entry_id} not yet filled (pre-market or low liquidity).\n"
            f"Stop at £{stop} will be placed automatically once the order fills.\n"
            f"Apex will check every 30 minutes."
        )
        # Spawn watchdog in the background to catch a quick late fill and place
        # the deferred stop without waiting for the 30-min cron cycle.
        #
        # Delay 90s before launch: we just burned 9 fill-poll API calls in this
        # process, and Cloudflare's per-IP burst counter is at its peak. Firing
        # the watchdog (which makes ~5–10 more T212 calls) immediately is what
        # caused the false STOP MISSING alert on 2026-04-16 — the watchdog hit
        # the rate-limit window, /equity/orders returned None, and every stop
        # appeared "missing".  90s lets the burst counter decay before retry.
        # See: scripts/CLAUDE.md "T212 Cloudflare Rate-Limit (Geo-1010)" lesson.
        try:
            import subprocess as _sp_deferred
            _sp_deferred.Popen(
                ['/bin/bash', '-c',
                 f'sleep 90 && {sys.executable} '
                 f'/home/ubuntu/.picoclaw/scripts/apex-broker-watchdog.py'],
                stdout=_sp_deferred.DEVNULL,
                stderr=_sp_deferred.DEVNULL,
                start_new_session=True,
            )
            _log("Watchdog spawned (delayed 90s) to monitor fill and place deferred stop")
        except Exception as _wd_e:
            log_warning(f"Could not spawn background watchdog: {_wd_e}")
        return True   # not an error — position is being managed

    # Use actual filled quantity for the stop (may differ from requested)
    neg_qty = round(filled_qty * -1, 8)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Place GTC stop-loss (3 attempts, rate limiter handles spacing)
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"Step 3: Placing STOP LOSS — {ticker} @ £{stop} for {filled_qty} shares")

    stop_id  = None
    attempts = 3

    for attempt in range(1, attempts + 1):
        _log(f"  Stop attempt {attempt}/{attempts}")
        stop_data = t212_request('/equity/orders/stop', method='POST', payload={
            "ticker":       ticker,
            "quantity":     neg_qty,
            "stopPrice":    t212_stop_price,
            "timeValidity": "GOOD_TILL_CANCEL",
        })
        stop_id = (stop_data or {}).get('id')
        if stop_id:
            break
        if attempt < attempts:
            backoff = [2, 8, 20][attempt - 1]
            time.sleep(backoff)   # exponential backoff: 2s → 8s → 20s (30s total covers 60s rate-limit window)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4a: Stop success → protected
    # ─────────────────────────────────────────────────────────────────────────
    if stop_id:
        _log(f"Step 3 OK: Stop ID {stop_id} @ £{stop}")
        _update_position(ticker, {
            'status':        'protected',
            'stop_order_id': str(stop_id),
            'unprotected':   False,
        })
        _audit(signal_id=None, ticker=ticker, signal_type=signal_type,
               from_state='entry_placed', to_state='protected',
               detail=f'stop placed @ £{stop}', t212_order_id=stop_id,
               filled_qty=filled_qty)

        # Append to TRADING_STATE.md
        try:
            with open(TRADING_STATE, 'a') as f:
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
                f.write(
                    f"{ts} | {order_type_used} BUY | {name} | {ticker} | "
                    f"qty:{quantity} | limit:{entry} | stop:{stop} (order:{stop_id}) | "
                    f"T1:{target1} | T2:{target2} | score:{score} | "
                    f"entry_id:{entry_id}\n"
                )
        except Exception as e:
            log_warning(f"TRADING_STATE.md write failed (non-fatal): {e}")

        # Remove signal file — consumed
        try:
            os.remove(PROCESSING_FILE)
        except FileNotFoundError:
            pass

        # ── Recovery ramp decrement ───────────────────────────────────────────
        # After a SUSPEND auto-resume, circuit breaker sets recovery_trades_remaining=N
        # and halves position sizing for that many trades.  Decrement here — the
        # executor is the only place that confirms a real trade was placed.
        try:
            from apex_utils import locked_read_modify_write as _lrmw
            _CB_FILE = '/home/ubuntu/.picoclaw/logs/apex-circuit-breaker.json'
            def _decrement_ramp(cb):
                cb = cb or {}
                ramp = cb.get('recovery_trades_remaining', 0)
                if ramp > 0:
                    cb['recovery_trades_remaining'] = ramp - 1
                    _log(f"Recovery ramp: {ramp} → {ramp - 1} trades remaining at reduced sizing")
                return cb
            _lrmw(_CB_FILE, _decrement_ramp, default={})
        except Exception as _ramp_e:
            log_warning(f"Recovery ramp decrement failed (non-blocking): {_ramp_e}")

        send_telegram(
            f"✅ TRADE PLACED\n"
            f"🏷 {name} ({ticker})\n"
            f"📐 Qty: {quantity} shares\n"
            f"💰 Entry: £{entry} ({order_type_used} — DAY order)\n"
            f"🛑 Stop: £{stop} (GTC — protected in T212)\n"
            f"🎯 T1: £{target1} | T2: £{target2}\n"
            f"📊 Score: {score}/10\n"
            f"🔖 Entry ID: {entry_id}\n"
            f"✅ Stop loss order placed (ID: {stop_id})\n\n"
            f"Your position is now protected even if Apex goes offline.\n"
            f"Reply CANCEL to cancel entry order."
        )

        # Step 6: Slippage check (non-blocking, brief wait for fill)
        time.sleep(3)
        try:
            fill_data = t212_request(f'/equity/orders/{entry_id}')
            if fill_data:
                actual_price = fill_data.get('fillPrice') or fill_data.get('limitPrice') or 0
                if actual_price:
                    import subprocess
                    subprocess.run(
                        ['python3',
                         '/home/ubuntu/.picoclaw/scripts/apex-slippage-tracker.py',
                         'log', name, ticker, str(entry), str(actual_price),
                         str(quantity), 'BUY', str(stop),
                         signal_type, str(score)],
                        capture_output=True
                    )
        except Exception:
            pass

        # Step 7: Export state to Gist for secondary watchdog (non-blocking)
        try:
            import subprocess as _sp
            _sp.Popen(
                ['python3', '/home/ubuntu/.picoclaw/scripts/apex-state-export.py'],
                stdout=open('/dev/null', 'w'), stderr=open('/dev/null', 'w')
            )
        except Exception:
            pass

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4b: Stop failed → unprotected — ALERT, do not close position
    # ─────────────────────────────────────────────────────────────────────────
    _log(f"CRITICAL: Stop loss FAILED for {ticker} after {attempts} attempts")

    # Create alert flag for health-check monitoring
    try:
        open(f'/home/ubuntu/.picoclaw/logs/STOP_MISSING_{ticker}', 'w').close()
    except Exception:
        pass

    # Find if pending entry already created the position record
    def _mark_unprotected(positions):
        positions = positions or []
        for p in positions:
            if (p.get('t212_ticker') == ticker
                    and p.get('status') in ('pending', 'entry_placed')):
                p['status']         = 'unprotected'
                p['entry_order_id'] = str(entry_id)
                p['stop_order_id']  = None
                p['unprotected']    = True
                return positions
        # Fallback: recreate if pending was somehow lost
        positions.append({
            "t212_ticker":    ticker,
            "name":           name,
            "quantity":       quantity,
            "entry":          entry,
            "stop":           stop,
            "target1":        target1,
            "target2":        target2,
            "score":          score,
            "rsi":            rsi,
            "macd":           macd,
            "sector":         sector,
            "atr":            atr,
            "signal_type":    signal_type,
            "currency":       currency,
            "fx_at_entry":    fx_at_entry,
            "opened":         today,
            "entry_order_id": str(entry_id),
            "stop_order_id":  None,
            "status":         "unprotected",
            "unprotected":    True,
            "order_type":     f"{order_type_used}+STOP",
            "regime_at_open": _regime_snapshot(),
            "regime_at_entry": safe_read(REGIME_FILE, {}).get('overall', 'UNKNOWN'),
        })
        return positions

    locked_read_modify_write(POSITIONS_FILE, _mark_unprotected, default=[])

    send_telegram(
        f"🚨 UNPROTECTED POSITION — ACTION REQUIRED\n\n"
        f"Ticker: {ticker} ({name})\n"
        f"Entry order placed (ID: {entry_id}) but STOP LOSS FAILED after {attempts} attempts.\n\n"
        f"⚠️ Position is OPEN with NO stop loss.\n"
        f"Log in to T212 and set a manual stop at £{stop}\n\n"
        f"To close: reply CLOSE {ticker}\n"
        f"To retry stop: log in to T212 app directly."
    )
    return False


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description='Apex Order Executor')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulate without placing real orders')
    parser.add_argument('--signal', default=SIGNAL_FILE,
                        help='Path to signal JSON file')
    args = parser.parse_args()

    # ── Read mode ONCE here and lock it for the entire execution ─────────────
    # Prevents a mode flip (PRACTICE → LIVE) mid-execution from silently
    # changing behaviour after the signal was already validated in PRACTICE.
    mode_at_entry = _get_mode()

    signal_path = args.signal
    processing_path = signal_path + '.processing'

    # ── Crash recovery: if .processing exists, a previous run crashed mid-execution ──
    # Do NOT start a new trade — the previous one may have partially executed.
    # Log a warning and exit so a human can investigate.
    if os.path.exists(processing_path):
        if os.path.exists(signal_path):
            # Both files exist — previous run crashed before cleanup, but a new
            # signal was also generated.  The .processing file is the dangerous one
            # (may have been partially executed).  Remove the NEW signal to prevent
            # double-entry, keep .processing for investigation.
            _log("WARNING: Both signal and .processing files exist — previous run crashed. "
                 "Removing new signal to prevent duplicate trade. Investigate .processing file.")
            send_telegram(
                "⚠️ DUPLICATE SIGNAL DETECTED\n\n"
                "A .processing file exists from a crashed run AND a new signal appeared.\n"
                "New signal removed. Investigate the .processing file manually."
            )
            try:
                os.remove(signal_path)
            except OSError:
                pass
            sys.exit(1)
        else:
            # Only .processing exists — previous run crashed mid-execution.
            _log("WARNING: .processing file exists from a crashed run — possible partial execution. "
                 "Will NOT re-execute. Investigate manually and delete .processing when resolved.")
            send_telegram(
                "⚠️ CRASHED EXECUTION DETECTED\n\n"
                "A .processing file exists from a previous run that crashed mid-trade.\n"
                "Check T212/Alpaca for partial fills. Delete .processing when resolved."
            )
            sys.exit(1)

    if not os.path.exists(signal_path):
        _log(f"ERROR: Signal file not found: {signal_path}")
        send_telegram("⚠️ No pending signal found.")
        sys.exit(1)

    try:
        signal = safe_read(signal_path, {})
    except Exception as e:
        _log(f"ERROR: Cannot read signal file: {e}")
        sys.exit(1)

    if not signal:
        _log("ERROR: Signal file is empty or invalid JSON")
        send_telegram("⚠️ Signal file empty or invalid.")
        sys.exit(1)

    # ── Signal TTL gate ───────────────────────────────────────────────────────
    # A signal generated at 08:30 is only valid for the current session.
    # If still pending after _SIGNAL_MAX_AGE_HOURS (default 6h), the thesis
    # may have changed — delete and force a fresh scan rather than executing
    # on stale entry assumptions.
    generated_at_str = signal.get('generated_at', '')
    if generated_at_str:
        try:
            generated_at = datetime.fromisoformat(
                generated_at_str.replace('Z', '+00:00'))
            age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600
            if age_hours > SIGNAL_MAX_AGE_HOURS:
                _log(f"SIGNAL EXPIRED: {age_hours:.1f}h old (max {SIGNAL_MAX_AGE_HOURS}h) — deleting")
                send_telegram(
                    f"⏰ SIGNAL EXPIRED — NOT EXECUTED\n\n"
                    f"{signal.get('name', '?')} ({signal.get('t212_ticker', '?')})\n"
                    f"Signal was {age_hours:.1f}h old (max {SIGNAL_MAX_AGE_HOURS}h).\n\n"
                    f"Signal deleted. Re-run morning scan to regenerate a fresh entry."
                )
                try:
                    os.remove(signal_path)
                except FileNotFoundError:
                    pass
                sys.exit(1)
        except Exception as _ttl_e:
            _log(f"WARNING: Cannot parse generated_at '{generated_at_str}': {_ttl_e}")

    # ── Atomic rename: claim the signal file before executing ─────────────────
    # os.rename() is atomic on POSIX (same filesystem).  If this fails, another
    # executor instance grabbed it first, or the disk is in trouble — either way
    # we must not proceed.
    try:
        os.rename(signal_path, processing_path)
    except OSError as e:
        _log(f"ERROR: Cannot rename signal to .processing — aborting to prevent duplicate: {e}")
        send_telegram(
            f"⚠️ EXECUTOR ABORTED — cannot claim signal file\n\n"
            f"os.rename failed: {e}\n"
            f"No trade placed. Investigate disk/permissions."
        )
        sys.exit(1)

    success = execute(signal, dry_run=args.dry_run, _mode=mode_at_entry)

    # ── Post-execution cleanup ───────────────────────────────────────────────
    # On failure, rename .processing back to the original path so retry is
    # possible on the next cycle.  On success, execute() already deleted the
    # .processing file — but clean up defensively in case it didn't.
    if not success:
        if os.path.exists(processing_path):
            try:
                os.rename(processing_path, signal_path)
                _log("Execution failed — signal file restored for retry")
            except OSError as e:
                _log(f"WARNING: Could not restore signal file after failure: {e}")
    else:
        # Defensive cleanup — execute() should have deleted .processing already
        if os.path.exists(processing_path):
            try:
                os.remove(processing_path)
            except OSError:
                pass

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
