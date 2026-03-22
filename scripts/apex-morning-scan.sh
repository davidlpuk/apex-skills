#!/bin/bash
# Data integrity check — block scan if critical failures
INTEGRITY=$(python3 /home/ubuntu/.picoclaw/scripts/apex-data-integrity.py quick 2>/dev/null)
if echo "$INTEGRITY" | grep -q "BLOCKED"; then
    echo "$(date): BLOCKED by data integrity check" >> /home/ubuntu/.picoclaw/logs/apex-cron.log
    echo "$INTEGRITY" >> /home/ubuntu/.picoclaw/logs/apex-cron.log
    exit 1
fi

# Reconcile positions before scanning
python3 /home/ubuntu/.picoclaw/scripts/apex-reconcile.py > /dev/null 2>&1
echo "$(date): Starting decision engine" >> /home/ubuntu/.picoclaw/logs/apex-cron.log
python3 /home/ubuntu/.picoclaw/scripts/apex-decision-engine.py >> /home/ubuntu/.picoclaw/logs/apex-cron.log 2>&1
echo "$(date): Decision engine complete" >> /home/ubuntu/.picoclaw/logs/apex-cron.log
