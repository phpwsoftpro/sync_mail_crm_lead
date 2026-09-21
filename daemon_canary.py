#!/usr/bin/env python3
"""daemon_canary.py — "watch the watchers": every 15 min make sure the mail loops are alive.

Why (2026-09-21): smart_mail_daemon.py hung for THREE DAYS (18/09 15:02 → 21/09 22:25) on a Gmail
socket Google had closed; launchd never re-launches a job it still sees as running, and nothing
else noticed because the sender/rescue/follow-up jobs kept working. 264 client mails never reached
a ticket. This job is the alarm that was missing.

Checks (all read-only unless a hang is proven):
  1. a `smart_mail_daemon.py` process older than MAX_RUN_MIN  -> kill -9 it, alert
  2. logs/sync_mail.log silent for STALE_DAEMON_MIN            -> alert (+ kickstart launchd job if no process)
  3. logs/send_reply.log silent for STALE_SENDER_MIN           -> alert (the sender ticks every 5 min)
  4. sender_bridge /health not answering                       -> kickstart com.syncmail.sender_bridge, alert
Alerts go to the Odoo "Mail Reports" channel (payroll.wsoftpro.com, channel 188) — the same place the
daemon posts its reports — at most once per hour per problem (state in .canary_state.json).

    ./venv/bin/python daemon_canary.py            # normal run (launchd com.syncmail.canary, 900 s)
    ./venv/bin/python daemon_canary.py --test     # post one "canary alive" line to the channel
"""
import json, os, subprocess, sys, time, urllib.request, xmlrpc.client, ssl

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(ROOT, "logs")
STATE = os.path.join(ROOT, ".canary_state.json")
MAX_RUN_MIN, STALE_DAEMON_MIN, STALE_SENDER_MIN, MAX_SENDER_RUN_MIN = 25, 20, 15, 10
DAEMON_LOG = "/tmp/smart_mail_daemon.log"                      # wrapper stdout (python -u -> live)
PROCESSED = os.path.join(ROOT, ".smart_mail_processed.json")   # rewritten at the END of every run
ALERT_COOLDOWN_S = 3600
PAYROLL_URL, PAYROLL_DB, CHANNEL = "https://payroll.wsoftpro.com", "29_5", 188


def load_env():
    try:
        for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(LOGS, "canary.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def post_alert(body):
    login, pw = os.environ.get("ODOO_LOGIN"), os.environ.get("ODOO_PASSWORD")
    if not login or not pw:
        log("ALERT (không đăng được: thiếu ODOO_LOGIN/ODOO_PASSWORD): " + body)
        return
    try:
        ctx = ssl.create_default_context()
        common = xmlrpc.client.ServerProxy(f"{PAYROLL_URL}/xmlrpc/2/common", context=ctx)
        uid = common.authenticate(PAYROLL_DB, login, pw, {})
        models = xmlrpc.client.ServerProxy(f"{PAYROLL_URL}/xmlrpc/2/object", context=ctx)
        models.execute_kw(PAYROLL_DB, uid, pw, "mail.channel", "message_post", [[CHANNEL]],
                          {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_comment"})
        log("đã đăng cảnh báo lên kênh 188")
    except xmlrpc.client.Fault as e:
        if "__dump" in str(e) or "marshal" in str(e):
            log("đã đăng cảnh báo lên kênh 188 (Odoo 16 không marshal được giá trị trả về — bình thường)")
        else:
            log(f"đăng cảnh báo thất bại: {str(e)[:120]}")
    except Exception as e:
        log(f"đăng cảnh báo thất bại: {str(e)[:120]}")


def daemon_processes():
    return script_processes("smart_mail_daemon.py")


def script_processes(script):
    """[(pid, elapsed_seconds)] for the python interpreter(s) actually running `script` —
    not a shell whose command line merely mentions it (a loop, or the canary itself)."""
    out = subprocess.run(["ps", "-axo", "pid=,etime=,command="], capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        argv = parts[2].split()
        if not argv or not os.path.basename(argv[0]).lower().startswith("python"):   # macOS: ".../Python.app/.../Python"
            continue
        if not any(a.endswith(script) for a in argv[1:3]):
            continue
        procs.append((int(parts[0]), _etime_seconds(parts[1])))
    return procs


def _etime_seconds(et):
    """ps etime: [[dd-]hh:]mm:ss"""
    days = 0
    if "-" in et:
        d, et = et.split("-", 1)
        days = int(d)
    f = [int(x) for x in et.split(":")]
    while len(f) < 3:
        f.insert(0, 0)
    return days * 86400 + f[0] * 3600 + f[1] * 60 + f[2]


def age_min(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 60
    except FileNotFoundError:
        return 1e9


def bridge_ok():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=5) as r:
            return b'"ok": true' in r.read()
    except Exception:
        return False


def main():
    load_env()
    os.makedirs(LOGS, exist_ok=True)
    if "--test" in sys.argv:
        post_alert("🐤 Canary đã bật trên Mac 32: kiểm tra daemon nhận mail / sender / bridge mỗi 15 phút, "
                   "tự giết tiến trình treo > 25 phút và báo ở đây. (Sau sự cố daemon treo 18→21/09.)")
        return
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    problems = []

    # "hung" = old process AND no log progress. A long legitimate run (backfill with many agy
    # calls) keeps writing sync_mail.log and must not be killed.
    log_age = age_min(DAEMON_LOG)
    for pid, el in daemon_processes():
        if el > MAX_RUN_MIN * 60 and log_age > STALE_DAEMON_MIN:
            try:
                os.kill(pid, 9)
                problems.append(("hung", f"smart_mail_daemon pid {pid} chạy {el//60} phút → đã kill -9 (launchd sẽ chạy lại ở tick sau)"))
            except Exception as e:
                problems.append(("hung", f"smart_mail_daemon pid {pid} chạy {el//60} phút, kill thất bại: {e}"))

    a = age_min(PROCESSED)   # a run finishes every 5 min -> this file is never older than ~10 min
    if a > STALE_DAEMON_MIN:
        if not daemon_processes():
            subprocess.run(["launchctl", "kickstart", "gui/501/com.syncmail.smart_mail"], capture_output=True)
            problems.append(("daemon_silent", f"daemon không hoàn tất lượt nào {a:.0f} phút, không có tiến trình → đã kickstart launchd"))
        elif log_age > STALE_DAEMON_MIN:
            problems.append(("daemon_silent", f"daemon không hoàn tất lượt nào {a:.0f} phút, log im {log_age:.0f} phút"))

    # the sender hung the same way on 21/09 (2 h 16 min holding .send_reply.lock -> every tick skipped)
    for pid, el in script_processes("send_reply_crm.py"):
        if el > MAX_SENDER_RUN_MIN * 60:
            try:
                os.kill(pid, 9)
                problems.append(("sender_hung", f"send_reply_crm pid {pid} chạy {el//60} phút → đã kill -9"))
            except Exception as e:
                problems.append(("sender_hung", f"send_reply_crm pid {pid} chạy {el//60} phút, kill thất bại: {e}"))

    b = age_min(os.path.join(LOGS, "send_reply.log"))
    if b > STALE_SENDER_MIN:
        problems.append(("sender_silent", f"send_reply.log im {b:.0f} phút (sender phải chạy mỗi 5 phút)"))

    if not bridge_ok():
        subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.syncmail.sender_bridge"], capture_output=True)
        problems.append(("bridge", "sender_bridge :8765 không trả lời → đã kickstart"))

    now = time.time()
    fresh = [(k, m) for k, m in problems if now - state.get(k, 0) > ALERT_COOLDOWN_S]
    for k, m in problems:
        log(f"{k}: {m}")
    if fresh:
        post_alert("🚨 Canary Mac 32:\n" + "\n".join(f"- {m}" for _, m in fresh))
        for k, _ in fresh:
            state[k] = now
    if not problems:
        log(f"OK last_daemon_run={a:.0f}m sender_log={b:.0f}m bridge=up procs={len(daemon_processes())}")
        for k in list(state):
            state.pop(k)
    json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    main()
