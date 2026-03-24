#!/bin/bash
# apex-data-refresher.sh
# Refreshes stale critical intelligence files before the morning scan.
# Called automatically by apex-morning-scan.sh when data integrity check fails.
# Also runs every Sunday at 20:00 UTC to prime data for Monday open.

LOGS="/home/ubuntu/.picoclaw/logs"
SCRIPTS="/home/ubuntu/.picoclaw/scripts"
CRON_LOG="$LOGS/apex-cron.log"

log() { echo "$(date -u '+%Y-%m-%d %H:%M UTC'): [REFRESHER] $1" | tee -a "$CRON_LOG"; }

log "Starting data refresh..."

# Map: log file → script that regenerates it
declare -A REFRESH_MAP=(
    ["apex-regime.json"]="apex-regime-check.py"
    ["apex-regime-scaling.json"]="apex-regime-scaling.py"
    ["apex-drawdown.json"]="apex-drawdown-check.py"
    ["apex-breadth-thrust.json"]="apex-breadth-thrust.py"
    ["apex-macro-signals.json"]="apex-macro-signals.py"
    ["apex-market-direction.json"]="apex-market-direction.py"
    ["apex-multiframe.json"]="apex-multiframe.py"
    ["apex-relative-strength.json"]="apex-relative-strength.py"
    ["apex-sentiment.json"]="apex-sentiment.py"
    ["apex-geo-news.json"]="apex-geo-news.py"
)

MAX_AGE_HOURS=26
refreshed=0
failed=0

for filename in "${!REFRESH_MAP[@]}"; do
    filepath="$LOGS/$filename"
    script="$SCRIPTS/${REFRESH_MAP[$filename]}"

    # Determine if file is stale or missing
    stale=0
    if [ ! -f "$filepath" ]; then
        stale=1
        log "MISSING: $filename"
    else
        age_seconds=$(( $(date +%s) - $(stat -c %Y "$filepath") ))
        age_hours=$(( age_seconds / 3600 ))
        if [ "$age_hours" -ge "$MAX_AGE_HOURS" ]; then
            stale=1
            log "STALE ($age_hours h): $filename"
        fi
    fi

    if [ "$stale" -eq 1 ]; then
        if [ -f "$script" ]; then
            log "Refreshing $filename via $script..."
            timeout 60 python3 "$script" >> "$CRON_LOG" 2>&1
            exit_code=$?
            if [ $exit_code -eq 0 ]; then
                log "  ✅ Refreshed: $filename"
                ((refreshed++))
            else
                log "  ❌ Failed (exit $exit_code): $script"
                ((failed++))
            fi
            sleep 1  # brief pause between scripts
        else
            log "  ⚠️  Script not found: $script"
            ((failed++))
        fi
    fi
done

log "Refresh complete — $refreshed refreshed, $failed failed"

# Return non-zero if any refreshes failed (caller can decide whether to proceed)
[ "$failed" -eq 0 ]
