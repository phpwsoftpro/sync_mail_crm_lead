#!/bin/bash
# syncmail - Sync all email accounts + compare with CRM
# Usage: syncmail          (sync + compare, report only)
#        syncmail --create (sync + compare + auto-create missing tickets)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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

echo "🔍 Comparing Gmail emails with CRM pipeline..."
echo ""
python3 compare_crm.py "$@"
