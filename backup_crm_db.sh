#!/bin/bash
# backup_crm_db.sh — nightly pg_dump of the CRM database (crm.wsoftpro.com, DB 27_05).
#
# Why (2026-09-21): trung merged ticket #460966 into #461772 by mistake; Odoo's merge unlinks the
# loser and there was NO backup anywhere, so the ticket number could not be brought back. This job
# makes that recoverable next time. launchd `com.syncmail.backup_crm_db`, 02:00 daily, keeps 14 days.
#
# Restore one row / one table from a dump (custom format, never touches the live DB by itself):
#   docker cp backups/27_05_YYYYmmdd.dump crm-internal-db-1:/tmp/x.dump
#   docker exec crm-internal-db-1 pg_restore -U odoo -d 27_05 -t crm_lead --data-only -f /tmp/crm_lead.sql /tmp/x.dump
set -euo pipefail
DIR=/Users/trung/CRM-Internal/backups
KEEP_DAYS=14
mkdir -p "$DIR"
stamp=$(date +%Y%m%d_%H%M)
out="$DIR/27_05_${stamp}.dump"
tmp="$out.part"
docker exec crm-internal-db-1 pg_dump -U odoo -Fc --no-owner 27_05 > "$tmp"
mv "$tmp" "$out"
size=$(du -h "$out" | cut -f1)
find "$DIR" -name '27_05_*.dump' -mtime +$KEEP_DAYS -delete
echo "$(date '+%Y-%m-%d %H:%M:%S') OK $out ($size) | kept: $(ls "$DIR"/27_05_*.dump | wc -l | tr -d ' ')"
