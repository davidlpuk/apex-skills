#!/bin/bash
# Reconcile positions before monitoring
python3 /home/ubuntu/.picoclaw/scripts/apex-reconcile.py > /dev/null 2>&1

# Check for T1 hits and execute partial closes
python3 << 'PYEOF2'
import subprocess, json, sys
sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

def load_env():
    env = {}
    with open('/home/ubuntu/.picoclaw/.env.trading212') as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

try:
    env      = load_env()
    auth     = env.get('T212_AUTH','')
    endpoint = env.get('T212_ENDPOINT','')
    result   = subprocess.run([
        'curl','-s','--max-time','10',
        '-H',f'Authorization: Basic {auth}',
        f'{endpoint}/equity/portfolio'
    ], capture_output=True, text=True)
    portfolio = json.loads(result.stdout)

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("pc", "/home/ubuntu/.picoclaw/scripts/apex-partial-close.py")
    _pc   = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_pc)
    actions = _pc.check_positions_for_t1(portfolio)
    if actions:
        print(f"Partial close actions: {len(actions)}")
except Exception as e:
    print(f"Partial close check error: {e}")
PYEOF2

LOG="/home/ubuntu/.picoclaw/logs/apex-stop-monitor.log"
echo "$(date): Running stop monitor" >> "$LOG"

# Run trailing stop manager — handles T1, T2, breakeven, stop hits
python3 /home/ubuntu/.picoclaw/scripts/apex-trailing-stop.py >> "$LOG" 2>&1

echo "$(date): Stop monitor complete" >> "$LOG"
