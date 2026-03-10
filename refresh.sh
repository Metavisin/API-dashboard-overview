#!/bin/bash
# MetaVision AI Cockpit — Hourly refresh
# Parses local OpenClaw logs → updates usage-data.json → pushes to GitHub
# Run via cron: 0 * * * * /Users/metavision/API-dashboard-overview/refresh.sh

set -euo pipefail

REPO="/Users/metavision/API-dashboard-overview"
LOG="$REPO/logs/refresh.log"
mkdir -p "$REPO/logs"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S CET')] $1" | tee -a "$LOG"; }

log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "🔄 AI Cockpit refresh starting"

cd "$REPO"
git pull origin main --quiet 2>>"$LOG" || log "⚠️  git pull failed (continuing)"

log "📊 Generating usage-data.json..."
python3 "$REPO/generate-data.py" 2>>"$LOG"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M CET')
git add usage-data.json
git diff --cached --quiet && { log "ℹ️  No changes to commit"; exit 0; }
git commit -m "data: hourly refresh — $TIMESTAMP" >>"$LOG" 2>&1
git push origin main >>"$LOG" 2>&1

log "✅ Pushed — dashboard live at https://metavisin.github.io/API-dashboard-overview/"
log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
