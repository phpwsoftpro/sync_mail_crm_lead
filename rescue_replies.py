#!/usr/bin/env python3
"""rescue_replies.py — the "Bot Cứu Hộ": move tickets back to Reply Client (3) when the client has
replied but the ticket is stuck in a dormant stage. Replaces the dead crm_daemon.py rescue role.

Why needed: Odoo fetchmail (create_uid=1) posts incoming client emails into the lead chatter but does
NOT change the stage, and it marks the Gmail message SEEN so smart_mail_daemon may never see it.
Without a rescue bot those replies sit unseen in Send Email Done / Done Follow Up / Old Lead
(2026-09-15: #462037 got two VESLOG replies and stayed in Send Email Done).

Rescue rule (workflow #462027 §3): a ticket in a dormant stage whose LATEST inbound email — from a
real client (not our own domains, not a system/vendor/no-reply/bounce/auto-reply sender) — is newer
than the ticket's last stage change → move to Reply Client (3), type=opportunity, post a note.

Bulk implementation (2026-09-15, replaces the per-lead scan that took >7 min over 2.2k leads):
  1 search_read of inbound emails younger than MAX_AGE_HOURS on crm.lead  →  group by lead
  1 search_read of those leads (dormant stages only)  →  compare dates  →  write per rescued lead.

  ./venv/bin/python rescue_replies.py                       # dry-run, 48 h window
  ./venv/bin/python rescue_replies.py --apply               # actually move (launchd com.syncmail.rescue, every 10 min)
  ./venv/bin/python rescue_replies.py --max-age-hours=0     # no window (historical audit — expect stragglers)
"""
import sys, datetime
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

DORMANT_STAGES = [9, 10, 34, 35, 19]      # Done Follow Up 1/2, Old Lead, Send Email Done, Unable to Send
REPLY_CLIENT = 3
OUR_DOMAINS = ("wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com", "musubiit.com")
# vendor/billing mailboxes are never a client reply even when the body looks human
EXTRA_SYSTEM_LOCALS = ("billing", "invoice", "payments", "payment", "receipt", "accounts", "noreply", "no-reply")
EXTRA_SYSTEM_DOMAINS = ("zohocorp.com", "zoho.com", "hubspot.com", "stripe.com", "paypal.com", "quickbooks.com", "xero.com", "atlassian.net", "atlassian.com")

def _arg(name, default):
    for a in sys.argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default

APPLY = "--apply" in sys.argv
MAX_AGE_HOURS = float(_arg("max-age-hours", "48"))

def rpc(sess, model, method, args, kwargs=None):
    r = sess.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/{model}/{method}",
                  json={"jsonrpc": "2.0", "method": "call",
                        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}},
                  timeout=60).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:160])
    return r.get("result")

def is_client_mail(m):
    frm = (m.get("email_from") or "").lower()
    if not frm or any(dom in frm for dom in OUR_DOMAINS):
        return False
    addr = frm.split("<")[-1].split(">")[0] if "<" in frm else frm
    local, _, domain = addr.partition("@")
    if d.is_system_sender(addr):
        return False
    if any(local.startswith(x) for x in EXTRA_SYSTEM_LOCALS) or any(domain == x or domain.endswith("." + x) for x in EXTRA_SYSTEM_DOMAINS):
        return False
    text = d.clean_html_body(m.get("body") or "")
    lab = d.fallback_classify(m.get("email_from", ""), m.get("subject", ""), text[:1500])
    return lab["stage"] not in d.JUNK_STAGE_KEYS

def main():
    sess = d.get_crm_session()
    dom = [["model", "=", "crm.lead"], ["message_type", "=", "email"]]
    if MAX_AGE_HOURS > 0:
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=MAX_AGE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        dom.append(["date", ">=", cutoff])
    msgs = rpc(sess, "mail.message", "search_read", [dom],
               {"fields": ["res_id", "date", "email_from", "subject", "body"], "order": "date desc", "limit": 5000})
    latest = {}                                   # lead id -> newest inbound email (any sender)
    for m in msgs:
        latest.setdefault(m["res_id"], m)
    if not latest:
        print("no inbound emails in window"); return
    leads = rpc(sess, "crm.lead", "search_read",
                [[["id", "in", list(latest)], ["active", "=", True], ["stage_id", "in", DORMANT_STAGES]]],
                {"fields": ["id", "name", "stage_id", "date_last_stage_update"], "limit": 100000})
    to_move, skipped_junk, skipped_old = [], 0, 0
    for l in leads:
        m = latest[l["id"]]
        if not (l["date_last_stage_update"] and m["date"] > l["date_last_stage_update"]):
            skipped_old += 1; continue            # mail predates the last stage move → already handled
        if not is_client_mail(m):
            skipped_junk += 1; continue
        to_move.append((l, m))
    print(f"{len(msgs)} inbound emails in window on {len(latest)} leads; {len(leads)} of those leads dormant; "
          f"{len(to_move)} to rescue (skipped {skipped_junk} junk/system, {skipped_old} older than last stage change)", flush=True)
    moved = 0
    for l, m in to_move:
        cur = (l["stage_id"] or [None, "?"])[1]
        print(f"  #{l['id']:6} {cur:24} <- {(m.get('email_from') or '')[:38]:38} {m['date'][:16]} | {l['name'][:40]}", flush=True)
        if APPLY:
            try:
                rpc(sess, "crm.lead", "write", [[l["id"]], {"stage_id": REPLY_CLIENT, "type": "opportunity"}])
                rpc(sess, "crm.lead", "message_post", [[l["id"]]],
                    {"body": f"🤖 Bot Cứu Hộ: khách rep lúc {m['date']} UTC (sau lần đổi cột {l['date_last_stage_update']}) → chuyển về Reply Client.",
                     "message_type": "comment", "subtype_xmlid": "mail.mt_note"})
                moved += 1
            except Exception as e:
                print(f"     FAIL: {e}", flush=True)
    print(("APPLIED, moved %d" % moved) if APPLY else "DRY-RUN (chạy --apply để thực hiện)", flush=True)

if __name__ == "__main__":
    main()
