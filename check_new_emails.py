#!/usr/bin/env python3
"""
Check New Emails — Output JSON for Antigravity to process.
Only detects new unread emails, does NOT classify or move anything.
Antigravity AI will handle classification and CRM actions.
"""
import sys, json, base64, datetime
sys.path.append("/Users/trung/syncmail-repo-auto")
import gmail_api_client

ACCOUNTS = [
    "robert@wsoftpro.com", "vanessa@wsoftpro.com", "supportteam@wsoftpro.com",
    "jennifer@hyperspacedev.com", "luna@hyperspacedev.com",
    "helen@interstellarsagency.com", "yuna@musubiit.com",
]

OUR_DOMAINS = ["wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com", "musubiit.com"]

PROCESSED_FILE = "/Users/trung/syncmail-repo-auto/.agy_processed.json"

def load_processed():
    try:
        with open(PROCESSED_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_processed(data):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f)

def get_body(payload):
    body = ""
    if "parts" in payload:
        for p in payload["parts"]:
            if p["mimeType"] == "text/plain" and "data" in p.get("body", {}):
                body += base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", errors="replace")
            elif "parts" in p:
                body += get_body(p)
    elif payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        body += base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    return body

def main():
    processed = load_processed()
    new_emails = []

    for account in ACCOUNTS:
        try:
            svc = gmail_api_client.get_gmail_service(account)
            res = svc.users().messages().list(userId="me", q="is:unread newer_than:10m", maxResults=5).execute()
            msgs = res.get("messages", [])
            for m in msgs:
                mid = m["id"]
                if mid in processed:
                    continue
                msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                sender = headers.get("From", "")
                sender_email = sender.lower()
                if "<" in sender_email:
                    sender_email = sender_email.split("<")[1].split(">")[0]
                if any(sender_email.endswith(d) for d in OUR_DOMAINS):
                    processed[mid] = "internal"
                    continue
                body = get_body(msg["payload"])
                new_emails.append({
                    "id": mid,
                    "account": account,
                    "from": sender,
                    "subject": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                    "body_preview": (body or msg.get("snippet", ""))[:600],
                })
                processed[mid] = datetime.datetime.now().isoformat()
        except Exception as e:
            pass

    save_processed(processed)
    print(json.dumps(new_emails, ensure_ascii=False))

if __name__ == "__main__":
    main()
