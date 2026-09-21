#!/usr/bin/env python3
"""sender_bridge.py — lets admin.wsoftpro.com ask THE sender to send one ticket now.

Why this exists (2026-09-21): admin_wsp must never send email itself (one sender rule — the client
would get the mail twice). Its "send" action writes the draft + stage 7 to Odoo and, until now, the
mail left on the next launchd tick of `send_reply_crm.py --auto` (<= 5 min). This bridge lets admin
trigger that SAME script for that ONE ticket immediately. Nothing is bypassed: the ticket must still
be in stage 7 with a draft, persona/thread selection is unchanged, and `send_reply_crm.py`'s flock
makes a bridge run and a launchd run take turns instead of double-sending.

    POST /send   {"odoo_lead_id": 462760}      Authorization: Bearer $SENDER_BRIDGE_TOKEN
      -> {"ok": true, "status": "sent"|"failed"|"not_in_queue"|"busy"|"error",
          "sent": n, "failed": n, "seconds": s, "log_tail": "..."}
    GET  /health -> {"ok": true}

Runs as launchd `com.syncmail.sender_bridge` on 0.0.0.0:8765; the admin_wsp container reaches it as
http://host.docker.internal:8765. Single-threaded on purpose: one send at a time.
"""
import hmac, json, os, re, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, "venv", "bin", "python")
SCRIPT = os.path.join(ROOT, "send_reply_crm.py")
PORT = int(os.environ.get("SENDER_BRIDGE_PORT", "8765"))


def _load_env():
    try:
        for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


_load_env()
TOKEN = os.environ.get("SENDER_BRIDGE_TOKEN", "")
if not TOKEN:
    print("SENDER_BRIDGE_TOKEN is not set in .env — refusing to start", flush=True)
    sys.exit(1)


def run_send(lead_id):
    t0 = time.time()
    try:
        p = subprocess.run([PY, SCRIPT, "--send", f"--lead={lead_id}"], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "error", "error": "sender timed out after 300s",
                "seconds": round(time.time() - t0)}
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"Results: (\d+) sent, (\d+) failed", out)
    sent, failed = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    if "holds the lock" in out:
        status = "busy"            # a launchd --auto run is in flight; it will send this ticket itself
    elif m and sent:
        status = "sent"
    elif m and failed:
        status = "failed"          # ticket moved to Unable to Send Email; details in the chatter
    elif "Found 0 tickets" in out:
        status = "not_in_queue"    # not in stage 7 / no draft / already sent by the --auto run
    else:
        status = "error"
    tail = "\n".join(l for l in out.splitlines() if l.strip())[-3000:]
    return {"ok": p.returncode == 0, "status": status, "sent": sent, "failed": failed,
            "exit_code": p.returncode, "seconds": round(time.time() - t0), "log_tail": tail}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}")

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"ok": False})

    def do_POST(self):
        if self.path != "/send":
            return self._json(404, {"ok": False})
        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            lead_id = int(body.get("odoo_lead_id") or 0)
        except Exception:
            lead_id = 0
        if lead_id <= 0:
            return self._json(400, {"ok": False, "error": "odoo_lead_id required"})
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} /send lead={lead_id} from {self.client_address[0]}", flush=True)
        res = run_send(lead_id)
        print(f"   -> {res['status']} in {res.get('seconds')}s", flush=True)
        self._json(200, res)

    def log_message(self, fmt, *args):   # keep the log to our own lines
        pass


if __name__ == "__main__":
    print(f"sender_bridge listening on 0.0.0.0:{PORT} (script: {SCRIPT})", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
