#!/bin/bash
# syncmail - Sync all email accounts + compare with CRM
# Usage: syncmail          (sync + compare, report only)
#        syncmail --create (sync + compare + auto-create missing tickets)
#        syncmail --quick  (sync only, no compare — used by scheduler)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

echo "📧 syncmail — Syncing all email accounts..."
echo ""

cd "$SCRIPT_DIR"
python3 fetch_emails_fast.py

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📁 JSON files:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
for f in "$SCRIPT_DIR/emails/"*.json; do
    count=$(python3 -c "import json; d=json.load(open('$f')); print(d['count'])" 2>/dev/null || echo "?")
    echo "  📄 $(basename $f)  ($count emails)"
done
echo ""

# Skip compare in quick mode (used by scheduler)
if [[ "$1" == "--quick" ]]; then
    echo "✅ Quick sync done (compare skipped)"
    exit 0
fi

echo "🔍 Comparing Gmail emails with CRM pipeline..."
echo ""
# Add 120s timeout to prevent hanging
timeout 120 python3 compare_crm.py "$@" || echo "⚠️ compare_crm timed out or failed"
