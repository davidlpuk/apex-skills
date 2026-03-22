#!/bin/bash

LOG="/home/ubuntu/.picoclaw/logs/apex-stop-monitor.log"
echo "$(date): Running stop monitor" >> "$LOG"

# Run trailing stop manager — handles T1, T2, breakeven, stop hits
python3 /home/ubuntu/.picoclaw/scripts/apex-trailing-stop.py >> "$LOG" 2>&1

echo "$(date): Stop monitor complete" >> "$LOG"
