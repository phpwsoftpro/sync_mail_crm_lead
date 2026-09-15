#!/bin/bash
cd /Users/trung/syncmail-repo-auto
export $(grep -v '^#' .env | xargs)
./venv/bin/python smart_mail_daemon.py >> /tmp/smart_mail_daemon.log 2>&1
