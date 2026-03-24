#!/usr/bin/env python3
import json
import sys
import subprocess
from datetime import datetime, timezone

import os

TICKER_MAP     = '/home/ubuntu/.picoclaw/scripts/apex-ticker-map.json'
QUALITY_FILE   = '/home/ubuntu/.picoclaw/scripts/apex-quality-universe.json'
SIGNAL_FILE    = '/home/ubuntu/.picoclaw/logs/apex-pending-signal.json'
MANUAL_STATE   = '/home/ubuntu/.picoclaw/logs/apex-manual-trade-state.json'
BREAKER_FILE   = '/home/ubuntu/.picoclaw/logs/apex-circuit-breaker.json'
GEO_FILE       = '/home/ubuntu/.picoclaw/logs/apex-geo-news.json'
POSITIONS_FILE = '/home/ubuntu/.picoclaw/logs/apex-positions.json'

def run_safety_gates(instrument, data):
    """
    Lightweight safety checks shown alongside the trade card.
    Returns (warnings, hard_blocks).
    Warnings are informational — trade can still proceed.
    Hard blocks prevent execution; user must override explicitly.
    """
    warnings    = []
    hard_blocks = []

    # 1. Circuit breaker state
    try:
        if os.path.exists(BREAKER_FILE):
            breaker = json.load(open(BREAKER_FILE))
            status  = breaker.get('status', 'CLEAR')
            pnl_pct = breaker.get('session_pnl_pct', 0)
            if status == 'CRITICAL':
                hard_blocks.append(
                    f"🚨 CIRCUIT BREAKER CRITICAL — session {pnl_pct:+.1f}%. All trading halted.")
            elif status == 'SUSPEND':
                hard_blocks.append(
                    f"🔴 CIRCUIT BREAKER SUSPEND — session {pnl_pct:+.1f}%. New entries blocked.")
            elif status == 'CAUTION':
                warnings.append(
                    f"🟠 Circuit breaker CAUTION — session {pnl_pct:+.1f}%. Sizing at 50%.")
            elif status == 'WARNING':
                warnings.append(
                    f"⚠️ Circuit breaker WARNING — session {pnl_pct:+.1f}%.")
    except Exception:
        pass

    # 2. Geo news — warn on energy instruments during active alerts
    try:
        if os.path.exists(GEO_FILE):
            geo     = json.load(open(GEO_FILE))
            overall = geo.get('overall', 'CLEAR')
            ENERGY  = {'XOM','CVX','SHEL','TTE','BP','XLE','IUES'}
            if overall == 'ALERT':
                if instrument in ENERGY and instrument in geo.get('energy_victims', []):
                    warnings.append(
                        f"⚠️ GEO ALERT: {geo.get('active_event','active event')} — "
                        f"{instrument} flagged as geo risk instrument.")
                else:
                    warnings.append(
                        f"⚠️ GEO ALERT active ({geo.get('active_event','')}) — elevated market risk.")
    except Exception:
        pass

    # 3. Position count and duplicate check
    try:
        positions = json.load(open(POSITIONS_FILE)) if os.path.exists(POSITIONS_FILE) else []
        if len(positions) >= 4:
            warnings.append(f"⚠️ Already holding {len(positions)} positions — portfolio fully allocated.")
        try:
            t212 = json.load(open(TICKER_MAP)).get(instrument, {}).get('t212', '')
            if t212 and any(p.get('t212_ticker') == t212 for p in positions):
                warnings.append(f"⚠️ You already hold {instrument} — this adds to an existing position.")
        except Exception:
            pass
    except Exception:
        pass

    # 4. Cash reserve check
    try:
        sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')
        from apex_utils import get_free_cash
        free_cash = get_free_cash() or 0
        notional  = data.get('notional', 0)
        if free_cash > 0 and notional > free_cash * 0.90:
            warnings.append(
                f"⚠️ Cash: trade needs £{notional:.2f} but 90% limit = "
                f"£{free_cash*0.9:.2f} (free: £{free_cash:.2f}). Reduce qty.")
        elif free_cash > 0 and notional > free_cash * 0.70:
            warnings.append(
                f"💡 This trade uses {notional/free_cash*100:.0f}% of free cash (£{free_cash:.2f}).")
    except Exception:
        pass

    return warnings, hard_blocks

# Natural language instrument recognition
INSTRUMENT_ALIASES = {
    # US Tech
    "apple":        "AAPL", "aapl":         "AAPL",
    "microsoft":    "MSFT", "msft":         "MSFT",
    "nvidia":       "NVDA", "nvda":         "NVDA",
    "google":       "GOOGL","alphabet":     "GOOGL", "googl": "GOOGL",
    "amazon":       "AMZN", "amzn":         "AMZN",
    "meta":         "META", "facebook":     "META",
    "tesla":        "TSLA", "tsla":         "TSLA",
    "salesforce":   "CRM",  "crm":          "CRM",
    "oracle":       "ORCL", "orcl":         "ORCL",
    "amd":          "AMD",  "intel":        "INTC", "intc": "INTC",
    "qualcomm":     "QCOM", "qcom":         "QCOM",
    # US Finance
    "jpmorgan":     "JPM",  "jp morgan":    "JPM",  "jpm": "JPM",
    "goldman":      "GS",   "goldman sachs":"GS",   "gs":  "GS",
    "morgan stanley":"MS",  "ms":           "MS",
    "bank of america":"BAC","bac":          "BAC",
    "blackrock":    "BLK",  "blk":          "BLK",
    "amex":         "AXP",  "american express":"AXP","axp": "AXP",
    "citigroup":    "C",    "citi":         "C",
    "visa":         "V",    "v":            "V",
    # US Healthcare
    "johnson":      "JNJ",  "j&j":          "JNJ",  "jnj": "JNJ",
    "pfizer":       "PFE",  "pfe":          "PFE",
    "merck":        "MRK",  "mrk":          "MRK",
    "unitedhealth": "UNH",  "unh":          "UNH",
    "abbvie":       "ABBV", "abbv":         "ABBV",
    "thermo fisher":"TMO",  "tmo":          "TMO",
    "danaher":      "DHR",  "dhr":          "DHR",
    # US Energy
    "exxon":        "XOM",  "exxon mobil":  "XOM",  "xom": "XOM",
    "chevron":      "CVX",  "cvx":          "CVX",
    # US Consumer
    "coca cola":    "KO",   "coke":         "KO",   "ko":  "KO",
    "pepsi":        "PEP",  "pepsico":      "PEP",  "pep": "PEP",
    "mcdonalds":    "MCD",  "mcdonald":     "MCD",  "mcd": "MCD",
    "walmart":      "WMT",  "wmt":          "WMT",
    "procter":      "PG",   "p&g":          "PG",   "pg":  "PG",
    # UK FTSE
    "hsbc":         "HSBA", "hsba":         "HSBA",
    "shell":        "SHEL", "shel":         "SHEL",
    "astrazeneca":  "AZN",  "azn":          "AZN",
    "unilever":     "ULVR", "ulvr":         "ULVR",
    "gsk":          "GSK",  "glaxo":        "GSK",
    "lloyds":       "LLOY", "lloy":         "LLOY",
    "bp":           "BP",
    "rio tinto":    "RIO",  "rio":          "RIO",
    "bae":          "BA",   "bae systems":  "BA",
    "relx":         "REL",  "rel":          "REL",
    "barclays":     "BARC", "barc":         "BARC",
    "natwest":      "NWG",  "nwg":          "NWG",
    "prudential":   "PRU",  "pru":          "PRU",
    "national grid":"NG",   "ng":           "NG",
    "diageo":       "DGE",  "dge":          "DGE",
    "vodafone":     "VOD",  "vod":          "VOD",
    "legal general":"LGEN", "legal and general":"LGEN","lgen":"LGEN",
    "aviva":        "AVIVA",
    # European
    "asml":         "ASML",
    "novo nordisk": "NOVO", "novo":         "NOVO",
    "novartis":     "NOVN", "novn":         "NOVN",
    "roche":        "ROG",  "rog":          "ROG",
    "airbus":       "AIR",  "air":          "AIR",
    "lvmh":         "LVMH", "louis vuitton":"LVMH",
    "santander":    "SAN",  "san":          "SAN",
    "siemens":      "SIE",  "sie":          "SIE",
    "totalenergies":"TTE",  "total":        "TTE",  "tte": "TTE",
    "sap":          "SAP",
    # ETFs
    "vanguard world":"VWRP","vwrp":         "VWRP",
    "vanguard sp500":"VUAG","vuag":         "VUAG",
    "sp500":        "VUAG", "s&p500":       "VUAG", "s&p 500": "VUAG",
    "gold":         "SGLN", "sgln":         "SGLN",
    "silver":       "SSLN", "ssln":         "SSLN",
    "energy etf":   "IUES", "iues":         "IUES",
    "nasdaq":       "EQQQ", "eqqq":         "EQQQ",
    "ftse":         "ISF",  "ftse 100":     "ISF",  "isf": "ISF",
}

def recognize_instrument(text):
    text_lower = text.lower().strip()
    # Direct match
    for alias, code in INSTRUMENT_ALIASES.items():
        if alias in text_lower:
            return code
    return None

def get_live_data(instrument_code):
    import yfinance as yf

    YAHOO_MAP = {
        "AAPL":"AAPL",  "MSFT":"MSFT",  "NVDA":"NVDA",  "GOOGL":"GOOGL",
        "AMZN":"AMZN",  "META":"META",  "TSLA":"TSLA",  "CRM":"CRM",
        "ORCL":"ORCL",  "AMD":"AMD",    "INTC":"INTC",  "QCOM":"QCOM",
        "JPM":"JPM",    "GS":"GS",      "MS":"MS",      "BAC":"BAC",
        "BLK":"BLK",    "AXP":"AXP",    "C":"C",        "V":"V",
        "JNJ":"JNJ",    "PFE":"PFE",    "MRK":"MRK",    "UNH":"UNH",
        "ABBV":"ABBV",  "TMO":"TMO",    "DHR":"DHR",    "XOM":"XOM",
        "CVX":"CVX",    "KO":"KO",      "PEP":"PEP",    "MCD":"MCD",
        "WMT":"WMT",    "PG":"PG",      "HSBA":"HSBA.L","SHEL":"SHEL.L",
        "AZN":"AZN.L",  "ULVR":"ULVR.L","GSK":"GSK.L", "LLOY":"LLOY.L",
        "BP":"BP.L",    "RIO":"RIO.L",  "BA":"BA.L",    "REL":"REL.L",
        "BARC":"BARC.L","NWG":"NWG.L",  "PRU":"PRU.L",  "NG":"NG.L",
        "DGE":"DGE.L",  "VOD":"VOD.L",  "LGEN":"LGEN.L","AVIVA":"AV.L",
        "ASML":"ASML.AS","NOVO":"NVO",  "NOVN":"NOVN.SW","ROG":"ROG.SW",
        "AIR":"AIR.PA", "LVMH":"MC.PA", "SAN":"SAN.MC", "SIE":"SIE.DE",
        "TTE":"TTE.PA", "VWRP":"VWRP.L","VUAG":"VUAG.L","SGLN":"SGLN.L",
        "SSLN":"SSLN.L","IUES":"IUFL.L","EQQQ":"EQQQ.L","ISF":"ISF.L",
    }

    CURRENCY_MAP = {
        "HSBA":"GBX","SHEL":"GBX","AZN":"GBX","ULVR":"GBX","GSK":"GBX",
        "LLOY":"GBX","BP":"GBX","RIO":"GBX","BA":"GBX","REL":"GBX",
        "BARC":"GBX","NWG":"GBX","PRU":"GBX","NG":"GBX","DGE":"GBX",
        "VOD":"GBX","LGEN":"GBX","AVIVA":"GBX","SGLN":"GBX","SSLN":"GBX",
        "VWRP":"GBP","VUAG":"GBP","EQQQ":"GBX","ISF":"GBX",
        "NOVN":"CHF","ROG":"CHF","AIR":"EUR","LVMH":"EUR","SAN":"EUR",
        "SIE":"EUR","TTE":"EUR","ASML":"EUR",
    }

    yahoo = YAHOO_MAP.get(instrument_code, instrument_code)
    currency = CURRENCY_MAP.get(instrument_code, "USD")

    try:
        t    = yf.Ticker(yahoo)
        hist = t.history(period="6mo")
        if hist.empty:
            return None

        close = hist['Close']
        if currency == "GBX":
            close = close.apply(lambda x: round(x/100, 2) if x > 100 else round(x, 2))

        price  = round(float(close.iloc[-1]), 2)
        ema50  = round(float(close.ewm(span=50).mean().iloc[-1]), 2)
        ema200 = round(float(close.ewm(span=200).mean().iloc[-1]), 2)

        delta  = close.diff()
        gain   = delta.where(delta > 0, 0).rolling(14).mean()
        loss   = -delta.where(delta < 0, 0).rolling(14).mean()
        rs     = gain / loss
        rsi    = round(float(100 - (100 / (1 + rs.iloc[-1]))), 2)

        hist_1y = t.history(period="1y")
        if currency == "GBX":
            hist_1y['Close'] = hist_1y['Close'].apply(lambda x: x/100 if x > 100 else x)
        high_52 = round(float(hist_1y['Close'].max()), 2)
        low_52  = round(float(hist_1y['Close'].min()), 2)
        discount = round((high_52 - price) / high_52 * 100, 1)

        # Stop at 6% below current
        stop = round(price * 0.94, 2)

        # Targets
        risk    = price - stop
        target1 = round(price + risk * 1.5, 2)
        target2 = round(price + risk * 2.5, 2)

        # Position size — £50 max risk
        qty     = max(1, round(50 / risk, 2))
        notional = round(qty * price, 2)
        if notional > 250:
            qty = round(250 / price, 2)
            notional = round(qty * price, 2)

        return {
            "code":      instrument_code,
            "price":     price,
            "currency":  currency,
            "rsi":       rsi,
            "ema50":     ema50,
            "ema200":    ema200,
            "high_52":   high_52,
            "low_52":    low_52,
            "discount":  discount,
            "stop":      stop,
            "target1":   target1,
            "target2":   target2,
            "quantity":  qty,
            "notional":  notional,
            "risk":      round(qty * risk, 2),
        }
    except Exception as e:
        return None

def save_manual_state(state):
    with open(MANUAL_STATE, 'w') as f:
        json.dump(state, f, indent=2)

def load_manual_state():
    try:
        with open(MANUAL_STATE) as f:
            return json.load(f)
    except:
        return {}

def clear_manual_state():
    try:
        import os
        os.remove(MANUAL_STATE)
    except:
        pass

def build_signal_from_manual(state, data):
    try:
        with open(TICKER_MAP) as f:
            tmap = json.load(f)
        t212 = tmap.get(state['instrument'], {}).get('t212', '')
        name = tmap.get(state['instrument'], {}).get('name', state['instrument'])
    except:
        t212 = ''
        name = state['instrument']

    signal = {
        "name":         name,
        "t212_ticker":  t212,
        "quantity":     state.get('quantity', data['quantity']),
        "entry":        data['price'],
        "stop":         state.get('stop', data['stop']),
        "target1":      data['target1'],
        "target2":      data['target2'],
        "score":        7,
        "rsi":          data['rsi'],
        "macd":         0,
        "sector":       "MANUAL",
        "signal_type":  "MANUAL",
        "currency":     data['currency'],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manual":       True
    }

    with open(SIGNAL_FILE, 'w') as f:
        json.dump(signal, f, indent=2)

    return signal

def process_message(text, bot_token, chat_id):
    def send(msg):
        import urllib.request, urllib.parse
        try:
            data = urllib.parse.urlencode({
                'chat_id': chat_id,
                'text': msg
            }).encode('utf-8')
            req = urllib.request.Request(
                f'https://api.telegram.org/bot{bot_token}/sendMessage',
                data=data
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"Send error: {e}")

    text_lower = text.lower().strip()
    state      = load_manual_state()

    # Check if we're in a conversation flow
    if state.get('awaiting'):
        awaiting = state['awaiting']

        if awaiting == 'confirm_instrument':
            if any(w in text_lower for w in ['yes','yeah','correct','yep','sure','ok','apex: yes','apex:yes','confirm']):
                send(f"⏳ Fetching live data for {state['instrument']}...")
                data = get_live_data(state['instrument'])
                if not data:
                    send(f"❌ Could not fetch data for {state['instrument']}. Please try again.")
                    clear_manual_state()
                    return True

                state['data']     = data
                state['awaiting'] = 'confirm_trade'
                save_manual_state(state)

                # Safety gates — run checks before showing card
                gate_warnings, gate_blocks = run_safety_gates(state['instrument'], data)

                # Hard block — don't proceed, inform user
                if gate_blocks:
                    block_msg = (
                        f"🚫 TRADE BLOCKED — {state['instrument']}\n\n"
                        + "\n".join(gate_blocks)
                        + "\n\nSend APEX RESUME to unlock, or check system status."
                    )
                    send(block_msg)
                    clear_manual_state()
                    return True

                rsi_note = "oversold ✅" if data['rsi'] < 35 else ("neutral" if data['rsi'] < 55 else "overbought ⚠️")
                trend    = "above" if data['price'] > data['ema50'] else "below"
                notional = round(data['quantity'] * data['price'], 2)
                risk_per_share = round(data['price'] - data['stop'], 2)

                # Prepend warnings to card if any
                warning_block = ""
                if gate_warnings:
                    warning_block = "\n".join(gate_warnings) + "\n\n"

                card = (
                    f"📊 {state['instrument']} — LIVE DATA\n\n"
                    + warning_block
                    + f"💰 Price: £{data['price']} {data['currency']}\n"
                    f"📈 RSI: {data['rsi']} ({rsi_note})\n"
                    f"📉 Trend: {trend} 50-day EMA (£{data['ema50']})\n"
                    f"📊 52w range: £{data['low_52']} – £{data['high_52']}\n"
                    f"⬇️ Down {data['discount']}% from 52w high\n\n"
                    f"📐 PROPOSED POSITION:\n"
                    f"  Quantity:  {data['quantity']} shares\n"
                    f"  Notional:  £{notional}\n"
                    f"  Stop loss: £{data['stop']} (£{risk_per_share}/share risk)\n"
                    f"  Max risk:  £{data['risk']}\n"
                    f"  Target 1:  £{data['target1']}\n"
                    f"  Target 2:  £{data['target2']}\n\n"
                    f"💡 Sizing: £50 max risk ÷ £{risk_per_share}/share = {data['quantity']} shares\n\n"
                    f"ADJUST QTY 3 — change quantity\n"
                    f"ADJUST STOP {data['stop']} — move stop\n"
                    f"CONFIRM — place order\n"
                    f"CANCEL — abort"
                )
                send(card)
                return True

            elif any(w in text_lower for w in ['no','nope','wrong','cancel','abort']):
                send("❌ Cancelled.")
                clear_manual_state()
                return True

        elif awaiting == 'confirm_trade':
            if any(w in text_lower for w in ['apex: confirm','apex:confirm','confirm']):
                data   = state.get('data', {})
                signal = build_signal_from_manual(state, data)

                if not signal.get('t212_ticker'):
                    send(f"⚠️ {state['instrument']} not found in T212 ticker map. Cannot place order automatically. Check T212 directly.")
                    clear_manual_state()
                    return True

                # Check market hours BEFORE placing order
                import datetime as _dt_mod2
                _now2      = _dt_mod2.datetime.now(_dt_mod2.timezone.utc)
                _hour_min2 = _now2.hour * 60 + _now2.minute
                _weekday2  = _now2.weekday() < 5
                _mkt_open2 = 480 <= _hour_min2 <= 930
                _is_open2  = _weekday2 and _mkt_open2

                if not _is_open2:
                    import json as _j2, subprocess as _sp2
                    with open('/home/ubuntu/.picoclaw/logs/apex-pending-signal.json','w') as _f2:
                        _j2.dump(signal, _f2, indent=2)
                    _sp2.run(['python3','/home/ubuntu/.picoclaw/scripts/apex-trade-queue.py','queue_signal'],
                             capture_output=True)
                    # Telegram message sent by queue_signal
                    clear_manual_state()
                    return True

                send(f"⏳ Placing order for {state['instrument']}...")

                result = subprocess.run(
                    ['bash', '/home/ubuntu/.picoclaw/scripts/apex-execute-order.sh'],
                    capture_output=True, text=True
                )

                if result.returncode == 0:
                    send(f"✅ MANUAL TRADE PLACED\n\n{signal['name']}\nEntry: £{signal['entry']} | Stop: £{signal['stop']}\nQty: {signal['quantity']} shares\n\nOrder placed in T212. Stop loss active.")
                else:
                    send(f"❌ Order failed. Check T212 directly.\n{result.stderr[:200]}")

                clear_manual_state()
                return True

            elif text_lower.startswith('adjust stop'):
                parts = text_lower.split()
                try:
                    new_stop = float(parts[-1])
                    state['stop'] = new_stop
                    save_manual_state(state)
                    send(f"✅ Stop updated to £{new_stop}. Type CONFIRM to place order or CANCEL to abort.")
                except:
                    send("⚠️ Usage: ADJUST STOP 155.00")
                return True

            elif text_lower.startswith('adjust qty'):
                parts = text_lower.split()
                try:
                    new_qty = float(parts[-1])
                    state['quantity'] = new_qty
                    save_manual_state(state)
                    data = state.get('data', {})
                    notional = round(new_qty * data.get('price', 0), 2)
                    risk     = round(new_qty * (data.get('price',0) - state.get('stop', data.get('stop',0))), 2)
                    send(f"✅ Quantity updated to {new_qty} shares (£{notional} notional, £{risk} risk). Type CONFIRM to place.")
                except:
                    send("⚠️ Usage: ADJUST QTY 2")
                return True

            elif any(w in text_lower for w in ['cancel','abort','no','stop']):
                send("❌ Trade cancelled.")
                clear_manual_state()
                return True

        return False

    # New trade request — detect BUY intent
    buy_triggers = ['apex: buy ', 'apex:buy ', 'buy ', 'purchase ', 'get ', 'i want ', "let's buy", 'invest in ', 'long ']
    is_buy = any(t in text_lower for t in buy_triggers)

    if not is_buy:
        return False

    # Recognize instrument
    instrument = recognize_instrument(text)

    if not instrument:
        send("🤔 I didn't recognise that instrument. Try:\n• buy Apple\n• buy Visa\n• buy AAPL\n• buy FTSE ETF\n\nOr use the ticker directly.")
        return True

    # Get instrument name
    try:
        with open(TICKER_MAP) as f:
            tmap = json.load(f)
        full_name = tmap.get(instrument, {}).get('name', instrument)
    except:
        full_name = instrument

    # Save state and ask confirmation
    state = {
        'instrument': instrument,
        'full_name':  full_name,
        'awaiting':   'confirm_instrument',
        'started_at': datetime.now(timezone.utc).isoformat()
    }
    save_manual_state(state)

    send(f"🤔 You want to buy {full_name} ({instrument})?\n\nReply TRADE YES to continue or NO to cancel.")
    return True

if __name__ == '__main__':
    if len(sys.argv) >= 4:
        text      = sys.argv[1]
        bot_token = sys.argv[2]
        chat_id   = sys.argv[3]
        handled   = process_message(text, bot_token, chat_id)
        print("HANDLED" if handled else "NOT_HANDLED")
    else:
        print("Usage: apex-manual-trade.py 'buy visa' BOT_TOKEN CHAT_ID")
