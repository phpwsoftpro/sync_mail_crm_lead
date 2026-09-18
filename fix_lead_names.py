#!/usr/bin/env python3
"""fix_lead_names.py — rename leads called "Email thread for <email>" to "<mail subject> [thread::<id>]".

Where the bad names come from: the Odoo addon `disable_contact_email_notify` patches
`mail.thread.message_route()` — when an incoming mail routes to a res.partner it redirects the mail
to that partner's newest CRM lead and, if there is none, CREATES one named
`f"Email thread for {partner.name}"`. No subject, no `[thread::id]`, no agy classification. 153 such
leads appeared 2026-09-16 → 18. A lead with no `[thread::id]` is exactly the case where the sender
has to guess the persona — that is how #463156 went out from Robert instead of Vanessa.

Name is rebuilt from the real data:
  subject  = the lead's first inbound chatter email (Odoo sometimes wraps it as "From X: X" — unwrapped)
  thread   = the Gmail thread of the newest mail from that client, looked up in the mailbox named by
             the lead's `Mail <persona>` tag first, then the rest

  ./venv/bin/python fix_lead_names.py                  # dry-run
  ./venv/bin/python fix_lead_names.py --apply [--limit=N] [--workers=3]
"""
import sys, re, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

APPLY = "--apply" in sys.argv
LIMIT = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--limit=")), "10000"))
WORKERS = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--workers=")), "3"))
TAG_TO_ACCOUNT = {34: "robert@wsoftpro.com", 35: "vanessa@wsoftpro.com",
                  75: "luna@hyperspacedev.com", 76: "helen@interstellarsagency.com",
                  82: "yuna@musubiit.com"}
_tls = threading.local()

def svc(acct):
    store = getattr(_tls, "s", None)
    if store is None:
        store = _tls.s = {}
    if acct not in store:
        store[acct] = d.gmail_api_client.get_gmail_service(acct)
    return store[acct]

def rpc(sess, model, method, args, kwargs=None):
    r = sess.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/{model}/{method}",
                  json={"jsonrpc": "2.0", "method": "call",
                        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}},
                  timeout=60).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:160])
    return r.get("result")

def clean_subject(subj):
    """Odoo sometimes stores the routed subject as 'From <subject>: <subject>' — keep the real one."""
    s = (subj or "").strip()
    m = re.match(r"^From\s+(.*?):\s*(.+)$", s)
    if m and m.group(2):
        s = m.group(2).strip()
    s = re.sub(r"\s*\[\s*thread::[^\]]*\]", "", s).strip()
    return s[:100]

def find_thread(addr, preferred):
    """Newest Gmail thread with this client — preferred mailbox first, then the others."""
    order = ([preferred] if preferred else []) + [a for a in d.ACCOUNTS if a != preferred]
    for acc in order:
        try:
            r = svc(acc).users().messages().list(
                userId="me", q=f"from:{addr} in:anywhere", maxResults=1).execute()
            msgs = r.get("messages") or []
            if not msgs:
                continue
            full = svc(acc).users().messages().get(
                userId="me", id=msgs[0]["id"], format="metadata", metadataHeaders=["Subject"]).execute()
            hdr = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            return full["threadId"], hdr.get("Subject", ""), acc
        except Exception:
            continue
    return None, "", None

def main():
    sess = d.get_crm_session()
    leads = rpc(sess, "crm.lead", "search_read",
                [[["name", "like", "Email thread for %"], ["active", "in", [True, False]]]],
                {"fields": ["id", "name", "email_from", "tag_ids"], "order": "id desc", "limit": LIMIT})
    print(f"{len(leads)} lead tên 'Email thread for …'", flush=True)
    ids = [l["id"] for l in leads]
    subjects = {}
    for i in range(0, len(ids), 200):
        for m in rpc(sess, "mail.message", "search_read",
                     [[["model", "=", "crm.lead"], ["res_id", "in", ids[i:i+200]], ["message_type", "=", "email"]]],
                     {"fields": ["res_id", "subject"], "order": "date asc", "limit": 5000}):
            if m.get("subject"):
                subjects.setdefault(m["res_id"], m["subject"])

    stats = {"renamed": 0, "no_thread": 0, "no_subject": 0, "err": 0}
    plan = []

    def work(l):
        addr = (l.get("email_from") or "").strip().lower()
        if "<" in addr:
            addr = addr.split("<")[1].split(">")[0]
        preferred = next((TAG_TO_ACCOUNT[t] for t in (l.get("tag_ids") or []) if t in TAG_TO_ACCOUNT), None)
        tid, gsubj, acc = find_thread(addr, preferred) if addr else (None, "", None)
        subj = clean_subject(gsubj) or clean_subject(subjects.get(l["id"]))
        if not subj:
            stats["no_subject"] += 1
            return
        name = f"{subj} [thread::{tid}]" if tid else subj
        if not tid:
            stats["no_thread"] += 1
        plan.append((l, name, acc))

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, leads))

    for l, name, acc in plan[:12]:
        print(f"  #{l['id']:6} {(l.get('email_from') or '')[:32]:32} -> {name[:70]}", flush=True)
    if len(plan) > 12:
        print(f"  … và {len(plan)-12} lead nữa", flush=True)
    print(f"đổi tên được: {len(plan)} | không tìm thấy thread: {stats['no_thread']} | không có subject: {stats['no_subject']}", flush=True)

    if APPLY:
        for l, name, acc in plan:
            try:
                rpc(sess, "crm.lead", "write", [[l["id"]], {"name": name}])
                stats["renamed"] += 1
                if stats["renamed"] % 25 == 0:
                    print(f"   đã đổi {stats['renamed']}…", flush=True)
            except Exception as e:
                stats["err"] += 1
                print(f"   FAIL #{l['id']}: {e}", flush=True)
        print(f"APPLIED: đổi tên {stats['renamed']}, lỗi {stats['err']}", flush=True)
    else:
        print("DRY-RUN (thêm --apply để thực hiện)", flush=True)

if __name__ == "__main__":
    main()
