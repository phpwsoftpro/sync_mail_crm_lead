#!/usr/bin/env python3
"""backfill_chatter_emails.py — for leads whose chatter has NO email from the client, fetch the client's
messages from Gmail (all 7 inboxes, incl. Spam/Trash) and post them into the chatter with their real dates.

  ./venv/bin/python backfill_chatter_emails.py --stages 3,7,6 [--max-per-lead 10] [--workers 3]

Only posts mail FROM the lead's sender address (our own outbound is logged elsewhere). Idempotent: a message whose
UTC timestamp already exists as an email message on the lead is skipped. Never sends mail, never changes stages.
"""
import sys, argparse, datetime, threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

_tls = threading.local()
def svc(acct):
    store = getattr(_tls, "s", None)
    if store is None:
        store = _tls.s = {}
    if acct not in store:
        store[acct] = d.gmail_api_client.get_gmail_service(acct)
    return store[acct]

def rpc(session, model, method, args, kwargs=None):
    r = session.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/{model}/{method}", json={"jsonrpc": "2.0", "method": "call",
        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}}, timeout=30).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:160])
    return r.get("result")

def client_messages(addr, max_per_lead):
    out = []
    for acct in d.ACCOUNTS:
        try:
            r = svc(acct).users().messages().list(userId="me", q=f"from:{addr} in:anywhere", maxResults=max_per_lead).execute()
            for m in r.get("messages") or []:
                full = svc(acct).users().messages().get(userId="me", id=m["id"], format="full").execute()
                h = {x["name"]: x["value"] for x in full["payload"]["headers"]}
                try:
                    dt = parsedate_to_datetime(h.get("Date")).astimezone(datetime.timezone.utc)
                except Exception:
                    dt = datetime.datetime.fromtimestamp(int(full.get("internalDate", 0)) / 1000, datetime.timezone.utc)
                out.append((dt, acct, full, h))
        except Exception as e:
            print(f"   gmail {acct} {addr}: {str(e)[:70]}", flush=True)
    # dedupe copies of the same message delivered to several inboxes (same Message-ID)
    seen, uniq = set(), []
    for item in sorted(out, key=lambda x: x[0]):
        mid = item[3].get("Message-ID") or item[3].get("Message-Id") or f"{item[0]}|{item[3].get('Subject')}"
        if mid in seen:
            continue
        seen.add(mid); uniq.append(item)
    return uniq[-max_per_lead:]

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--stages", default="3,7,6"); ap.add_argument("--max-per-lead", type=int, default=10)
    ap.add_argument("--workers", type=int, default=3); a = ap.parse_args()
    session = d.get_crm_session()
    stages = [int(x) for x in a.stages.split(",")]
    leads = rpc(session, "crm.lead", "search_read", [[["active", "=", True], ["stage_id", "in", stages]]],
                {"fields": ["id", "name", "email_normalized", "email_from"], "order": "id desc", "limit": 100000})
    # keep only leads with no email-type message yet
    ids = [l["id"] for l in leads]
    have = set()
    for i in range(0, len(ids), 500):
        for g in rpc(session, "mail.message", "read_group",
                     [[["model", "=", "crm.lead"], ["res_id", "in", ids[i:i+500]], ["message_type", "=", "email"]], ["res_id"], ["res_id"]]):
            have.add(g["res_id"])
    todo = [l for l in leads if l["id"] not in have and (l["email_normalized"] or l["email_from"])]
    print(f"{len(leads)} leads in {stages}; {len(todo)} without any client email in chatter", flush=True)
    stats = {"leads_filled": 0, "messages_posted": 0, "nothing_found": 0, "err": 0}

    def work(l):
        addr = (l["email_normalized"] or l["email_from"] or "").strip().lower()
        if "<" in addr: addr = addr.split("<")[1].split(">")[0]
        try:
            msgs = client_messages(addr, a.max_per_lead)
            if not msgs:
                stats["nothing_found"] += 1; return
            n = 0
            for dt, acct, full, h in msgs:
                dt_utc = dt.strftime("%Y-%m-%d %H:%M:%S")
                if rpc(session, "mail.message", "search_count", [[["model", "=", "crm.lead"], ["res_id", "=", l["id"]], ["message_type", "=", "email"], ["date", "=", dt_utc]]]):
                    continue
                body = d.get_email_body(full["payload"]); html = d.get_email_body_html(full["payload"])
                if d.post_email_to_chatter(session, l["id"], h.get("From", ""), h.get("Subject", ""), body or full.get("snippet", ""), html, dt_utc):
                    n += 1
            if n:
                stats["leads_filled"] += 1; stats["messages_posted"] += n
                if stats["leads_filled"] % 25 == 0: print(f"   progress: {stats}", flush=True)
        except Exception as e:
            stats["err"] += 1; print(f"   error lead {l['id']}: {str(e)[:100]}", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(work, todo))
    print("DONE", stats, flush=True)

if __name__ == "__main__":
    main()
