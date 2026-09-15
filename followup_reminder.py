#!/usr/bin/env python3
"""followup_reminder.py — "Bot Nhắc Follow-up" (daily, 08:00 VN via launchd com.syncmail.followup_reminder).

Rule (trung, 2026-09-15): an email WE sent from the system > 3 days ago with NO client reply since
→ move the ticket to Reply Client (3) so Sales writes the next follow-up from there.
Replaces the old Bot Cảnh Sát target (Old Lead cần Follow-up 34) — that column was a graveyard nobody worked.

Candidates: active tickets in the "sent" stages 35 Send Email Done / 9 Done Follow Up 1 / 10 Done Follow Up 2.
"Sent date" = the latest "📧 Email sent to …" comment (fallback: date_last_stage_update).
"Client replied" = an inbound email (message_type=email, not from our domains, not a system/junk sender) newer
than the sent date → those are Bot Cứu Hộ's job (rescue_replies.py), NOT this bot's; they are skipped here.

  ./venv/bin/python followup_reminder.py                      # dry-run, stages 35,9 (workflow-defined next steps)
  ./venv/bin/python followup_reminder.py --apply              # actually move + post a note
  ./venv/bin/python followup_reminder.py --stages=35,9,10     # include Done Follow Up 2 (no defined next step —
                                                              #   82 tickets, mostly months old; trung decides)
"""
import sys, datetime
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

SENT_STAGES = {35: "Send Email Done", 9: "Done Follow Up 1", 10: "Done Follow Up 2"}
NEXT_ACTION = {35: "cần follow-up lần 1", 9: "cần follow-up lần 2",
               10: "đã follow-up 2 lần vẫn im — quyết định: nhắc lần 3 hay Reject"}
REPLY_CLIENT = 3
STALE_DAYS = 3
OUR_DOMAINS = ("wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com", "musubiit.com")
APPLY = "--apply" in sys.argv

def stages_arg():
    for a in sys.argv:
        if a.startswith("--stages="):
            return [int(x) for x in a.split("=", 1)[1].split(",") if x.strip()]
    return [35, 9]   # default: only the stages whose next step the workflow defines

def rpc(sess, model, method, args, kwargs=None):
    r = sess.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/{model}/{method}",
                  json={"jsonrpc": "2.0", "method": "call",
                        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}},
                  timeout=30).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:160])
    return r.get("result")

def is_client_mail(m):
    frm = (m.get("email_from") or "").lower()
    if any(dom in frm for dom in OUR_DOMAINS):
        return False
    addr = frm.split("<")[-1].split(">")[0] if "<" in frm else frm
    if d.is_system_sender(addr):
        return False
    text = d.clean_html_body(m.get("body") or "")
    lab = d.fallback_classify(m.get("email_from", ""), m.get("subject", ""), text[:1500])
    return lab["stage"] not in d.JUNK_STAGE_KEYS

def main():
    sess = d.get_crm_session()
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    stages = [s for s in stages_arg() if s in SENT_STAGES]
    leads = rpc(sess, "crm.lead", "search_read",
                [[["active", "=", True], ["stage_id", "in", stages]]],
                {"fields": ["id", "name", "email_from", "stage_id", "date_last_stage_update"], "limit": 100000})
    to_move, waiting, replied = [], 0, 0
    for l in leads:
        sent = rpc(sess, "mail.message", "search_read",
                   [[["model", "=", "crm.lead"], ["res_id", "=", l["id"]], ["message_type", "=", "comment"],
                     ["body", "ilike", "Email sent to"]]],
                   {"fields": ["date"], "order": "date desc", "limit": 1})
        sent_date = sent[0]["date"] if sent else l["date_last_stage_update"]
        if not sent_date:
            continue
        # any genuine client reply after we sent? -> rescue territory, skip
        inbound = rpc(sess, "mail.message", "search_read",
                      [[["model", "=", "crm.lead"], ["res_id", "=", l["id"]], ["message_type", "=", "email"],
                        ["date", ">", sent_date]]],
                      {"fields": ["email_from", "subject", "body"], "order": "date desc", "limit": 3})
        if any(is_client_mail(m) for m in inbound):
            replied += 1; continue
        if sent_date > cutoff:
            waiting += 1; continue
        days = (datetime.datetime.utcnow() - datetime.datetime.strptime(sent_date, "%Y-%m-%d %H:%M:%S")).days
        to_move.append((l, sent_date, days))

    print(f"{len(leads)} tickets in sent stages | {len(to_move)} silent > {STALE_DAYS} days → Reply Client "
          f"| {waiting} still within {STALE_DAYS} days | {replied} client replied (Bot Cứu Hộ's job)", flush=True)
    moved = 0
    for l, sent_date, days in sorted(to_move, key=lambda x: -x[2]):
        sid = l["stage_id"][0]
        print(f"  #{l['id']:6} {SENT_STAGES[sid]:17} sent {sent_date[:10]} ({days:2}d) | {NEXT_ACTION[sid][:28]:28} | {l['name'][:38]}", flush=True)
        if APPLY:
            try:
                rpc(sess, "crm.lead", "write", [[l["id"]], {"stage_id": REPLY_CLIENT, "type": "opportunity"}])
                rpc(sess, "crm.lead", "message_post", [[l["id"]]],
                    {"body": f"⏰ Bot Nhắc Follow-up: mail gửi {sent_date[:10]} ({days} ngày) khách chưa rep "
                             f"→ chuyển về Reply Client, {NEXT_ACTION[sid]}.",
                     "message_type": "comment", "subtype_xmlid": "mail.mt_note"})
                moved += 1
            except Exception as e:
                print(f"     FAIL: {e}", flush=True)
    print(("APPLIED, moved %d" % moved) if APPLY else "DRY-RUN (chạy --apply để thực hiện)", flush=True)

if __name__ == "__main__":
    main()
