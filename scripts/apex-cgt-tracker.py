#!/usr/bin/env python3
import json
from datetime import datetime, timezone

OUTCOMES_FILE = '/home/ubuntu/.picoclaw/logs/apex-outcomes.json'
CGT_FILE      = '/home/ubuntu/.picoclaw/logs/apex-cgt.json'

def calculate_cgt():
    try:
        with open(OUTCOMES_FILE) as f:
            db = json.load(f)
    except:
        print("No outcomes data yet")
        return

    trades = db.get('trades', [])
    if not trades:
        print("No completed trades yet")
        return

    tax_year_start = "2026-04-06"
    tax_year_end   = "2027-04-05"

    gains    = []
    losses   = []
    total_proceeds    = 0
    total_cost        = 0

    for t in trades:
        closed = t.get('closed', '')
        if not (tax_year_start <= closed <= tax_year_end):
            continue

        proceeds = t['exit'] * t['quantity']
        cost     = t['entry'] * t['quantity']
        gain     = proceeds - cost

        total_proceeds += proceeds
        total_cost     += cost

        if gain > 0:
            gains.append({
                "name":     t['name'],
                "closed":   closed,
                "gain":     round(gain, 2),
                "proceeds": round(proceeds, 2),
                "cost":     round(cost, 2)
            })
        else:
            losses.append({
                "name":     t['name'],
                "closed":   closed,
                "loss":     round(abs(gain), 2),
                "proceeds": round(proceeds, 2),
                "cost":     round(cost, 2)
            })

    total_gains  = sum(g['gain'] for g in gains)
    total_losses = sum(l['loss'] for l in losses)
    net_gain     = total_gains - total_losses
    cgt_allowance = 3000
    taxable      = max(0, net_gain - cgt_allowance)
    cgt_basic    = round(taxable * 0.18, 2)
    cgt_higher   = round(taxable * 0.24, 2)

    print(f"\n💷 CGT SUMMARY — Tax Year 2026/27")
    print(f"{'='*45}")
    print(f"Total proceeds:     £{round(total_proceeds, 2):>10,.2f}")
    print(f"Total cost:         £{round(total_cost, 2):>10,.2f}")
    print(f"Total gains:        £{round(total_gains, 2):>10,.2f}")
    print(f"Total losses:       £{round(total_losses, 2):>10,.2f}")
    print(f"Net gain/loss:      £{round(net_gain, 2):>10,.2f}")
    print(f"CGT allowance:      £{cgt_allowance:>10,.2f}")
    print(f"Taxable amount:     £{round(taxable, 2):>10,.2f}")
    print(f"{'='*45}")
    print(f"CGT at 18% (basic): £{cgt_basic:>10,.2f}")
    print(f"CGT at 24% (higher):£{cgt_higher:>10,.2f}")

    if gains:
        print(f"\n📈 Gains ({len(gains)} trades):")
        for g in sorted(gains, key=lambda x: x['gain'], reverse=True)[:5]:
            print(f"  {g['name']:20} £{g['gain']:>8,.2f}  ({g['closed']})")

    if losses:
        print(f"\n📉 Losses ({len(losses)} trades):")
        for l in sorted(losses, key=lambda x: x['loss'], reverse=True)[:5]:
            print(f"  {l['name']:20} £{l['loss']:>8,.2f}  ({l['closed']})")

    if taxable == 0:
        print(f"\n✅ Within CGT allowance — no tax due this year")
    else:
        print(f"\n⚠️ CGT may be due — consult your accountant")

    # FX warning for USD positions
    usd_trades = [t for t in trades if t.get('currency') == 'USD']
    if usd_trades:
        print(f"\n⚠️ FX NOTE: {len(usd_trades)} USD trades — HMRC requires GBP conversion")
        print(f"   at the exchange rate on the date of each transaction.")
        print(f"   Apex currently logs USD prices — you'll need to apply")
        print(f"   the correct GBP/USD rate for each trade date.")

    # Save to CGT file
    cgt_report = {
        "tax_year":      "2026/27",
        "generated":     datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        "net_gain":      round(net_gain, 2),
        "taxable":       round(taxable, 2),
        "cgt_basic":     cgt_basic,
        "cgt_higher":    cgt_higher,
        "gains":         gains,
        "losses":        losses,
        "usd_trades":    len(usd_trades)
    }

    with open(CGT_FILE, 'w') as f:
        json.dump(cgt_report, f, indent=2)

    print(f"\n📄 Full report saved to apex-cgt.json")

if __name__ == '__main__':
    calculate_cgt()
