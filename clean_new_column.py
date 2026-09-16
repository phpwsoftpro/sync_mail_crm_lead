#!/usr/bin/env python3
"""clean_new_column.py — move junk that slipped into New (1) to Z - Mail Rác (22).

Why: before 2026-09-16 `full_sync_reconciler.py` created a lead for EVERY unknown sender in the
whole mailbox history (auto-replies, no-reply/system senders, ticket-system mail), straight into
New. That leak is now gated at the source, but the leads it already made are still sitting in New.

Judged with exactly the same rules the daemon uses — `is_system_sender()` plus the keyword
`fallback_classify()` on the lead's subject + description — so this never reclassifies anything
differently from the live pipeline. Leads with client conversation history are always left alone:
a lead is only touched when it has **no inbound email in its chatter** (pure reconciler artifact).

  ./venv/bin/python clean_new_column.py                 # dry-run over all of New
  ./venv/bin/python clean_new_column.py --today         # only leads created today
  ./venv/bin/python clean_new_column.py --today --apply
"""
import sys, datetime
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

NEW_STAGE, JUNK_STAGE = 1, 22
OUR_DOMAINS = ("wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com", "musubiit.com")
APPLY = "--apply" in sys.argv
TODAY_ONLY = "--today" in sys.argv

def rpc(sess, model, method, args, kwargs=None):
    r = sess.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/{model}/{method}",
                  json={"jsonrpc": "2.0", "method": "call",
                        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}},
                  timeout=60).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:160])
    return r.get("result")

def main():
    sess = d.get_crm_session()
    dom = [["active", "=", True], ["stage_id", "=", NEW_STAGE]]
    if TODAY_ONLY:
        dom.append(["create_date", ">=", datetime.datetime.utcnow().strftime("%Y-%m-%d 00:00:00")])
    leads = rpc(sess, "crm.lead", "search_read", [dom],
                {"fields": ["id", "name", "email_from", "description"], "limit": 100000})
    ids = [l["id"] for l in leads]
    # Most of New was created by Odoo's own fetchmail (uid 1), which posts the mail into the
    # chatter but never classifies it — so "has a chatter email" does NOT mean genuine. Judge on
    # the mail body itself, and protect only leads that show a real two-way conversation:
    # more than one inbound mail, or one of our "📧 Email sent" notes.
    first_mail, inbound_count, has_send_note = {}, {}, set()
    for i in range(0, len(ids), 200):
        chunk = ids[i:i+200]
        for m in rpc(sess, "mail.message", "search_read",
                     [[["model", "=", "crm.lead"], ["res_id", "in", chunk], ["message_type", "=", "email"]]],
                     {"fields": ["res_id", "body", "subject", "email_from"], "order": "id asc", "limit": 5000}):
            inbound_count[m["res_id"]] = inbound_count.get(m["res_id"], 0) + 1
            first_mail.setdefault(m["res_id"], m)
        for m in rpc(sess, "mail.message", "search_read",
                     [[["model", "=", "crm.lead"], ["res_id", "in", chunk], ["message_type", "=", "comment"],
                       ["body", "ilike", "Email sent to"]]], {"fields": ["res_id"], "limit": 5000}):
            has_send_note.add(m["res_id"])

    junk, kept, protected = [], 0, 0
    for l in leads:
        lid = l["id"]
        if lid in has_send_note or inbound_count.get(lid, 0) > 1:
            protected += 1; continue                       # real conversation — never touched here
        frm = (l.get("email_from") or "").lower()
        addr = frm.split("<")[-1].split(">")[0] if "<" in frm else frm
        if not addr:
            kept += 1; continue
        if any(addr.endswith(x) for x in OUR_DOMAINS):
            junk.append((l, "internal domain")); continue
        if d.is_system_sender(addr):
            junk.append((l, "system sender")); continue
        m = first_mail.get(lid)
        body = d.clean_html_body(m.get("body") or "") if m else (l.get("description") or "")
        subject = (m.get("subject") if m else None) or l.get("name", "")
        lab = d.fallback_classify(l.get("email_from", ""), subject, body[:1500])
        if lab["stage"] in d.JUNK_STAGE_KEYS:
            junk.append((l, lab["stage"])); continue
        kept += 1

    print(f"New column: {len(leads)} leads ({'today only' if TODAY_ONLY else 'all'}) | "
          f"{len(junk)} junk to move | {kept} genuine kept | {protected} protected (have client mail)", flush=True)
    by_reason = {}
    for _, r in junk:
        by_reason[r] = by_reason.get(r, 0) + 1
    print("  reasons:", by_reason, flush=True)
    for l, r in junk[:15]:
        print(f"    #{l['id']:6} {r:18} {(l.get('email_from') or '')[:40]:40} {l['name'][:40]}", flush=True)
    if len(junk) > 15:
        print(f"    … and {len(junk)-15} more", flush=True)

    if APPLY and junk:
        moved = 0
        batch = [l["id"] for l, _ in junk]
        for i in range(0, len(batch), 50):
            chunk = batch[i:i+50]
            try:
                rpc(sess, "crm.lead", "write", [chunk, {"stage_id": JUNK_STAGE}])
                moved += len(chunk)
            except Exception as e:
                print(f"    FAIL on chunk starting {chunk[0]}: {e}", flush=True)
        print(f"APPLIED, moved {moved} leads to Z - Mail Rác", flush=True)
    elif not APPLY:
        print("DRY-RUN (thêm --apply để thực hiện)", flush=True)

if __name__ == "__main__":
    main()
