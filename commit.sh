#!/bin/bash
# Quick commit before making changes
# Usage: ./commit.sh "description of changes"
cd ~/.picoclaw
MSG="${1:-checkpoint $(date '+%Y-%m-%d %H:%M')}"
git add scripts/ logs/
git commit -m "$MSG"
echo "✅ Committed: $MSG"
git log --oneline -5
