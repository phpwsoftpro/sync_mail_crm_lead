#!/usr/bin/env python3
"""backfill_source_tags.py — give every lead exactly ONE "Tag Nguồn Mail" (Mail Robert/Vanessa/Luna/Helen/Yuna/…):
the inbox that actually received the client's email.

  ./venv/bin/python backfill_source_tags.py --stages 3,7,6               # untagged leads in those columns
  ./venv/bin/python backfill_source_tags.py --stages 1,34 --workers 4      # bigger backfill (background)
  ./venv/bin/python backfill_source_tags.py --normalize --stages all       # ALSO fix leads carrying >=2 Mail tags

How the source inbox is chosen (one only):
  1. lead name carries [thread::<gmail thread id>]  -> the inbox that owns that thread id (threads().get succeeds)
  2. else: the inbox holding the MOST RECENT message *from* the sender (never `to:` — that is our outreach)
Read-only on Gmail (metadata only). CRM write = set the Mail tag (unlink other Mail tags, keep non-Mail tags).
"""
import sys, re, argparse, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

TAG_BY_ACCOUNT = {"robert@wsoftpro.com": 34, "vanessa@wsoftpro.com": 35, "luna@hyperspacedev.com": 75,
                  "helen@interstellarsagency.com": 76, "yuna@musubiit.com": 82}
TAG_NAME_BY_ACCOUNT = {"supportteam@wsoftpro.com": "Mail Supportteam", "jennifer@hyperspacedev.com": "Mail Jennifer"}
MAIL_TAG_IDS = set(TAG_BY_ACCOUNT.values())
lock = threading.Lock()
_tls = threading.local()   # googleapiclient objects are not thread-safe -> one per thread per account

def svc(acct):
    store = getattr(_tls, "services", None)
    if store is None:
        store = _tls.services = {}
    if acct not in store:
        store[acct] = d.gmail_api_client.get_gmail_service(acct)
    return store[acct]

def rpc(session, model, method, args, kwargs=None):
    r = session.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/{model}/{method}", json={"jsonrpc": "2.0", "method": "call",
        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}}, timeout=30).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:160])
    return r.get("result")

def load_mail_tags(session):
    """All crm.tag ids whose name starts with 'Mail ' (so we can unlink the extras)."""
    for t in rpc(session, "crm.tag", "search_read", [[["name", "ilike", "Mail %"]]], {"fields": ["id", "name"]}):
        MAIL_TAG_IDS.add(t["id"])
        for acct, name in TAG_NAME_BY_ACCOUNT.items():
            if t["name"] == name:
                TAG_BY_ACCOUNT[acct] = t["id"]

def ensure_tag(session, acct):
    if acct in TAG_BY_ACCOUNT:
        return TAG_BY_ACCOUNT[acct]
    name = TAG_NAME_BY_ACCOUNT.get(acct)
    if not name:
        return None
    with lock:
        if acct not in TAG_BY_ACCOUNT:
            tid = rpc(session, "crm.tag", "create", [{"name": name}])
            TAG_BY_ACCOUNT[acct] = tid; MAIL_TAG_IDS.add(tid)
        return TAG_BY_ACCOUNT[acct]

def owner_of_thread(thread_id):
    for acct in d.ACCOUNTS:
        try:
            svc(acct).users().threads().get(userId="me", id=thread_id, format="minimal").execute()
            return acct
        except Exception:
            continue
    return None

def inbox_with_latest_mail_from(addr):
    best, best_ts = None, -1
    for acct in d.ACCOUNTS:
        try:
            r = svc(acct).users().messages().list(userId="me", q=f"from:{addr} in:anywhere", maxResults=1).execute()
            msgs = r.get("messages") or []
            if not msgs:
                continue
            m = svc(acct).users().messages().get(userId="me", id=msgs[0]["id"], format="minimal").execute()
            ts = int(m.get("internalDate", 0))
            if ts > best_ts:
                best, best_ts = acct, ts
        except Exception as e:
            print(f"   gmail error {acct} {addr}: {str(e)[:80]}", flush=True)
    return best

def source_inbox(lead, cache):
    m = re.search(r"thread::([0-9a-f]{10,})", lead.get("name") or "")
    if m:
        acct = owner_of_thread(m.group(1))
        if acct:
            return acct
    addr = (lead["email_normalized"] or lead["email_from"] or "").strip().lower()
    if "<" in addr:
        addr = addr.split("<")[1].split(">")[0]
    if not addr:
        return None
    if addr not in cache:
        acct = inbox_with_latest_mail_from(addr)
        if not acct and "@" in addr:
            # exact address not found (sender rewrites / list addresses) -> try the domain
            acct = inbox_with_latest_mail_from("@" + addr.split("@", 1)[1])
        cache[addr] = acct
    return cache[addr]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="3,7,6"); ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--normalize", action="store_true", help="also re-tag leads that carry >=2 Mail tags")
    ap.add_argument("--limit", type=int, default=100000)
    a = ap.parse_args()
    session = d.get_crm_session(); load_mail_tags(session)
    dom = [["active", "=", True]]
    if a.stages != "all":
        dom.append(["stage_id", "in", [int(x) for x in a.stages.split(",")]])
    leads = rpc(session, "crm.lead", "search_read", [dom],
                {"fields": ["id", "name", "email_normalized", "email_from", "tag_ids"], "order": "id desc", "limit": a.limit})
    def mail_tags(l): return [t for t in l["tag_ids"] if t in MAIL_TAG_IDS]
    todo = [l for l in leads if (len(mail_tags(l)) == 0 or (a.normalize and len(mail_tags(l)) >= 2))
            and (l["email_normalized"] or l["email_from"])]
    print(f"{len(leads)} leads scanned; {len(todo)} to (re)tag "
          f"({sum(1 for l in todo if len(mail_tags(l))>=2)} with >=2 tags, {sum(1 for l in todo if not mail_tags(l))} untagged)", flush=True)
    cache, stats = {}, {"tagged": 0, "unchanged": 0, "no_inbox": 0, "err": 0}

    def work(l):
        try:
            acct = source_inbox(l, cache)
            tid = ensure_tag(session, acct) if acct else None
            current = mail_tags(l)
            if not tid:
                if len(current) >= 2:
                    # can't resolve the inbox but the lead must not keep several sources:
                    # keep the one already attached first (earliest link), drop the rest
                    tid = current[0]
                else:
                    stats["no_inbox"] += 1; return
            if current == [tid]:
                stats["unchanged"] += 1; return
            ops = [(3, t) for t in current if t != tid] + ([] if tid in current else [(4, tid)])
            rpc(session, "crm.lead", "write", [[l["id"]], {"tag_ids": ops}])
            stats["tagged"] += 1
            if stats["tagged"] % 50 == 0:
                print(f"   progress: {stats}", flush=True)
        except Exception as e:
            stats["err"] += 1; print(f"   error lead {l['id']}: {e}", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(work, todo))
    print("DONE", stats, flush=True)

if __name__ == "__main__":
    main()
