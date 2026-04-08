#!/usr/bin/env python3
"""
apex-t212-history-sync.py — Fetch all Trading 212 historical orders via API.

Writes to logs/apex-t212-history.json:
  {
    "orders": [ <normalised flat order dicts, FILLED only> ],
    "synced_at": "2026-03-27T07:05:00Z",
    "total_fetched": 42,
    "pages": 3
  }

T212 API returns each item as {order: {...}, fill: {...}} — we normalise to a
flat dict so that import_t212_api_history() in the tax tracker can parse it.

Pagination: T212 uses cursor-based pagination via `nextPagePath` in the response.
Rate limit: 6 req/min for history endpoints — we wait 11s between pages.

After writing, automatically POSTs to the local tax dashboard to import new trades
so the CGT position is always up to date without any manual button press.

Run via cron: 07:05 (pre-market), 09:01 (market open), 16:50 (EOD) weekdays.
Also triggered on-demand from the tax dashboard.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# Add scripts dir to path for apex_utils
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_utils import t212_request, log_error, log_warning, atomic_write, LOG_DIR

OUTPUT_FILE = os.path.join(LOG_DIR, 'apex-t212-history.json')
# Strip this prefix from nextPagePath if present (T212 returns /api/v0/... paths)
API_PATH_PREFIX = '/api/v0'
# Pause between pages to respect 6 req/min rate limit
PAGE_DELAY_SECONDS = 11
# Local tax dashboard import endpoint
TAX_IMPORT_URL = 'http://127.0.0.1:7777/tax/import/t212-api'


def _strip_api_prefix(path: str) -> str:
    """Convert /api/v0/equity/history/orders?cursor=x → /equity/history/orders?cursor=x"""
    if path.startswith(API_PATH_PREFIX):
        return path[len(API_PATH_PREFIX):]
    return path


def _normalize_order(item: dict) -> dict | None:
    """
    T212 wraps each history item as {order: {...}, fill: {...}}.
    Normalise to a flat dict that import_t212_api_history() can parse.
    Returns None if the order is not FILLED.

    Key fields produced:
      id, ticker, name, direction (BUY/SELL), status, filledQuantity,
      filledPrice, dateCreated (fill execution time), currency,
      netValueGbp (actual GBP cash — post-FX, post-fee),
      fxRate (T212 execution rate), commissionGbp (FX fees, already in netValueGbp)
    """
    # Handle both nested {order, fill} format and any future flat format
    order_obj = item.get('order', item)
    fill_obj  = item.get('fill', {})

    status = order_obj.get('status', '').upper()
    if status != 'FILLED':
        return None

    instrument = order_obj.get('instrument', {})
    wallet     = fill_obj.get('walletImpact', {})

    # Sum FX/tax charges in GBP — already deducted from netValue; recorded for audit
    taxes = wallet.get('taxes', [])
    commission_gbp = sum(
        abs(float(t.get('quantity', 0)))
        for t in taxes
        if t.get('currency') == 'GBP'
    )

    return {
        'id':             order_obj['id'],
        'ticker':         order_obj.get('ticker') or instrument.get('ticker', ''),
        'name':           instrument.get('name', order_obj.get('ticker', '')),
        'direction':      order_obj.get('side', '').upper(),    # BUY / SELL
        'status':         status,
        'quantity':       order_obj.get('filledQuantity', 0),   # signed (neg = sell)
        'filledQuantity': abs(float(order_obj.get('filledQuantity', 0))),
        'filledPrice':    fill_obj.get('price', 0),
        # Use fill execution time — this is the CGT disposal/acquisition date
        'dateCreated':    fill_obj.get('filledAt') or order_obj.get('createdAt', ''),
        'currency':       instrument.get('currency', ''),        # USD / GBP / GBX
        # Actual GBP cash received/paid — post-FX, post-fee; use for immediate CGT calc
        # For USD instruments: avoids waiting for HMRC monthly rate lookup
        'netValueGbp':    wallet.get('netValue'),
        'fxRate':         wallet.get('fxRate'),                  # T212 execution rate
        'commissionGbp':  commission_gbp,                        # FX fees (already in netValueGbp)
    }


def fetch_all_orders() -> dict:
    """
    Paginate through all T212 historical orders.
    Returns dict with all FILLED orders (normalised to flat dicts) + metadata.
    """
    all_orders = []
    pages = 0
    path = '/equity/history/orders'

    while path:
        data = t212_request(path)
        if data is None:
            log_error(f"apex-t212-history-sync: API call failed on path {path}")
            break

        items = data.get('items', [])
        pages += 1

        for item in items:
            # T212 returns {order: {...}, fill: {...}} — normalise to flat dict
            flat = _normalize_order(item)
            if flat is not None:
                all_orders.append(flat)

        next_path = data.get('nextPagePath')
        if next_path:
            path = _strip_api_prefix(next_path)
            # Respect rate limit: max 6 req/min = 11s between requests
            time.sleep(PAGE_DELAY_SECONDS)
        else:
            break

    return {
        'orders': all_orders,
        'synced_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'total_fetched': len(all_orders),
        'pages': pages,
    }


def _trigger_tax_import() -> None:
    """
    POST to the local tax dashboard to import from the freshly synced history file.
    This ensures the CGT tracker is always up to date without any manual button press.
    Non-fatal: if the dashboard is unreachable the sync data is still written to disk
    and will be imported on the next dashboard load.
    """
    try:
        req = urllib.request.Request(
            TAX_IMPORT_URL,
            data=b'action=import-only',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        # Follow redirects; dashboard returns 302 → dashboard page after import
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[apex-t212-history-sync] Tax import triggered: HTTP {resp.status}")
    except Exception as e:
        print(f"[apex-t212-history-sync] Tax import trigger failed (non-fatal): {e}")


def main():
    print(f"[apex-t212-history-sync] Fetching T212 historical orders...")

    result = fetch_all_orders()

    if atomic_write(OUTPUT_FILE, result):
        print(
            f"[apex-t212-history-sync] Done: {result['total_fetched']} FILLED orders "
            f"across {result['pages']} page(s) → {OUTPUT_FILE}"
        )
        # Auto-import into tax DB — no manual button press needed
        _trigger_tax_import()
        sys.exit(0)
    else:
        print(f"[apex-t212-history-sync] ERROR: Failed to write {OUTPUT_FILE}")
        sys.exit(1)


if __name__ == '__main__':
    main()
