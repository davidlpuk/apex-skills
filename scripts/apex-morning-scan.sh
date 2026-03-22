#!/bin/bash
# Reconcile positions before scanning
python3 /home/ubuntu/.picoclaw/scripts/apex-reconcile.py > /dev/null 2>&1
echo "$(date): Starting decision engine" >> /home/ubuntu/.picoclaw/logs/apex-cron.log
python3 /home/ubuntu/.picoclaw/scripts/apex-decision-engine.py >> /home/ubuntu/.picoclaw/logs/apex-cron.log 2>&1
echo "$(date): Decision engine complete" >> /home/ubuntu/.picoclaw/logs/apex-cron.log
