#!/bin/bash
# launchd wrapper for smart_mail_daemon.py (every 300 s).
# 2026-09-21 watchdog: a run hung for 3 days on a dead Gmail socket and launchd never restarted it
# (a "running" job is never re-launched). Kill any run older than 25 min so the next tick can start.
cd /Users/trung/syncmail-repo-auto
export $(grep -v '^#' .env | xargs)
./venv/bin/python smart_mail_daemon.py >> /tmp/smart_mail_daemon.log 2>&1 &
PY_PID=$!
( sleep 1500; if kill -0 $PY_PID 2>/dev/null; then echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG: killing hung smart_mail_daemon pid $PY_PID" >> logs/sync_mail.log; kill -9 $PY_PID; fi ) &
WD=$!
wait $PY_PID; RC=$?
kill $WD 2>/dev/null
exit $RC
