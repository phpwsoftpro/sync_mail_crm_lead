#!/usr/bin/env python3
"""reclassify_recent.py — Re-check the last N days of inbound mail with Antigravity Pro (agy),
compare with each lead's current CRM column and (optionally) fix it.

  ./venv/bin/python reclassify_recent.py --days 2            # phase 1: classify + plan (no CRM writes)
  ./venv/bin/python reclassify_recent.py --apply plan.json   # phase 2: apply a reviewed plan

Routing rules mirror smart_mail_daemon.py / workflow #462027:
  human reply (reply_client|proposition|checking_meeting) -> Reply Client (3) unless already in 3 or 7
  new_lead                                                 -> New (1) only if the lead sits in a junk column
  junk                                                     -> Z - Mail Rác (22) only if the lead sits in New (1) or Reply Client (3)
  no lead yet                                              -> create in New (1) for human/new_lead labels; junk is not created
Working columns (7, 35, 9, 10, 34, 5, 6, 13, 16, 8, 15, 17, 11, 14, 4) are never touched except by a human-reply rescue.
"""
import sys, os, json, re, datetime, argparse
from concurrent.futures import ThreadPoolExecutor
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

OUT_DIR = "/Users/trung/syncmail-repo-auto/_review_20260911"
os.makedirs(OUT_DIR, exist_ok=True)
JUNK_COLUMNS = {22, 18, 20, 21, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32}
KEEP_COLUMNS = {3, 7}                      # already where a reply belongs / draft queued
OUR_DOMAINS = ("wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com", "musubiit.com")
d.AGY_MAX_CALLS_PER_RUN = 10**9            # no per-run cap for this one-off batch

def addr(sender):
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", sender or "")
    return m.group(0).lower() if m else (sender or "").lower()

def fetch_recent(account, days):
    svc = d.gmail_api_client.get_gmail_service(account)
    q = f"newer_than:{days}d"
    msgs, token = [], None
    while True:
        r = svc.users().messages().list(userId="me", q=q, maxResults=200, pageToken=token).execute()
        msgs += r.get("messages", [])
        token = r.get("nextPageToken")
        if not token or len(msgs) > 1500:
            break
    out = []
    for m in msgs:
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        h = {x["name"]: x["value"] for x in full["payload"]["headers"]}
        sender = h.get("From", "")
        a = addr(sender)
        if any(a.endswith(dom) for dom in OUR_DOMAINS):
            continue
        body = d.get_email_body(full["payload"])
        html = d.get_email_body_html(full["payload"])
        text = body or re.sub(r"<[^>]+>", " ", html or "") or full.get("snippet", "")
        text = re.sub(r"[ \t]+", " ", text).strip()
        out.append({"id": m["id"], "account": account, "from": sender, "email": a,
                    "subject": h.get("Subject", "(no subject)"), "date": h.get("Date", ""),
                    "text": text, "html": html})
    return out

def classify(mail):
    r = d.classify_with_agy(mail["from"], mail["subject"], mail["text"], mail["account"])
    if not r:
        r = d.fallback_classify(mail["from"], mail["subject"], mail["text"])
        r["reason"] = "[keyword fallback — agy failed] " + r["reason"]
    return r

def strength(stage):
    if stage in d.HUMAN_REPLY_KEYS: return 3
    if stage == "new_lead": return 2
    return 1

def plan(days):
    mails = []
    for acct in d.ACCOUNTS:
        try:
            got = fetch_recent(acct, days)
            print(f"{acct}: {len(got)} external mails", flush=True)
            mails += got
        except Exception as e:
            print(f"{acct}: fetch error {str(e)[:120]}", flush=True)
    print(f"total {len(mails)} mails -> classifying with agy (4 workers)...", flush=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        labels = list(ex.map(classify, mails))
    session = d.get_crm_session()
    rows, by_sender = [], {}
    for m, lab in zip(mails, labels):
        m["label"] = lab["stage"]; m["reason"] = lab["reason"]
        m["is_junk"] = (lab["stage"] in d.JUNK_STAGE_KEYS) or d.is_system_sender(m["email"])
        m["is_reply"] = (not m["is_junk"]) and lab["stage"] in d.HUMAN_REPLY_KEYS
        k = m["email"]
        # strongest signal per sender wins; ties -> most recent mail
        if k not in by_sender or strength(lab["stage"]) > strength(by_sender[k]["label"]) or \
           (strength(lab["stage"]) == strength(by_sender[k]["label"]) and m["date"] > by_sender[k]["date"]):
            by_sender[k] = m
    for k, m in by_sender.items():
        lead = d.find_existing_lead(session, k)
        cur = (lead.get("stage_id") or [None, None]) if lead else [None, None]
        cur_id, cur_name = cur[0], cur[1]
        action, target = "keep", None
        if lead is None:
            if m["is_reply"] or m["label"] == "new_lead":
                action, target = "create", 1
        elif m["is_reply"]:
            if cur_id not in KEEP_COLUMNS:
                action, target = "rescue", 3
        elif m["label"] == "new_lead" and not m["is_junk"]:
            if cur_id in JUNK_COLUMNS:
                action, target = "un-junk", 1
        elif m["is_junk"]:
            if cur_id in (1, 3):
                action, target = "junk", 22
        rows.append({"email": k, "from": m["from"], "subject": m["subject"], "date": m["date"], "account": m["account"],
                     "label": m["label"], "reason": m["reason"], "lead": lead["id"] if lead else None,
                     "lead_type": lead.get("type") if lead else None, "cur_stage_id": cur_id, "cur_stage": cur_name,
                     "action": action, "target": target, "gmail_id": m["id"], "n_mails": sum(1 for x in mails if x["email"] == k)})
    rows.sort(key=lambda r: ({"rescue": 0, "create": 1, "un-junk": 2, "junk": 3, "keep": 4}[r["action"]], r["email"]))
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    pj = os.path.join(OUT_DIR, f"reclassify_plan_{ts}.json")
    json.dump({"days": days, "mails": len(mails), "senders": len(rows), "rows": rows}, open(pj, "w"), ensure_ascii=False, indent=1)
    # markdown summary
    md = [f"# Reclassify plan — last {days} days — {len(mails)} mails / {len(rows)} senders\n",
          "| action | lead | now | → | label | sender | subject |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["action"] == "keep": continue
        md.append(f"| {r['action']} | {r['lead'] or '—'} | {r['cur_stage'] or '—'} | {r['target']} | {r['label']} | {r['email']} | {r['subject'][:50]} |")
    counts = {}
    for r in rows: counts[r["action"]] = counts.get(r["action"], 0) + 1
    md.append(f"\ncounts: {counts}")
    open(pj.replace(".json", ".md"), "w").write("\n".join(md))
    print(f"\nPLAN: {counts}\nsaved {pj}", flush=True)
    return pj

def apply(plan_path):
    p = json.load(open(plan_path)); session = d.get_crm_session()
    done, fail = 0, 0
    for r in p["rows"]:
        if r["action"] == "keep": continue
        try:
            if r["action"] == "create":
                tag = d.tag_for_account(r["account"])
                vals = {"name": r["subject"][:100], "email_from": r["email"], "contact_name": r["from"].split("<")[0].strip().strip('"') or r["email"],
                        "stage_id": 1, "type": "lead"}
                if tag: vals["tag_ids"] = [(4, tag)]
                res = session.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/create", json={"jsonrpc": "2.0", "method": "call",
                      "params": {"model": "crm.lead", "method": "create", "args": [vals], "kwargs": {}}}).json()
                lid = res.get("result"); ok = bool(lid)
                if ok:
                    d.post_email_to_chatter(session, lid, r["from"], r["subject"], f"[reclassify] {r['reason']}", "")
            else:
                res = session.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/write", json={"jsonrpc": "2.0", "method": "call",
                      "params": {"model": "crm.lead", "method": "write", "args": [[r["lead"]], {"stage_id": r["target"]}], "kwargs": {}}}).json()
                ok = res.get("result") is True
                if ok:
                    session.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/message_post", json={"jsonrpc": "2.0", "method": "call",
                        "params": {"model": "crm.lead", "method": "message_post", "args": [[r["lead"]]],
                                   "kwargs": {"body": f"🤖 Re-classified by Antigravity Pro (last {p['days']} days review): {r['label']} — {r['reason']}. "
                                                      f"Moved {r['cur_stage']} → stage {r['target']}.", "message_type": "comment", "subtype_xmlid": "mail.mt_note"}}})
            done += ok; fail += (not ok)
            print(("OK  " if ok else "FAIL"), r["action"], r["lead"], r["email"], flush=True)
        except Exception as e:
            fail += 1; print("FAIL", r["action"], r["lead"], r["email"], str(e)[:100], flush=True)
    print(f"applied {done}, failed {fail}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=2); ap.add_argument("--apply")
    a = ap.parse_args()
    apply(a.apply) if a.apply else plan(a.days)
