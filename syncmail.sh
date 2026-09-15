#!/bin/bash
# syncmail - Sync all email accounts + compare with CRM
# (REWRITTEN to use Gmail API)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

echo "📧 syncmail — Syncing all email accounts via GMAIL API..."
echo ""

cd "$SCRIPT_DIR"

if [[ "$1" == "--quick" || "$1" == "--dry-run" ]]; then
    python3 sync_mail_api.py --dry-run
else
    python3 sync_mail_api.py
fi

echo "✅ Sync complete via API"
exit 0
