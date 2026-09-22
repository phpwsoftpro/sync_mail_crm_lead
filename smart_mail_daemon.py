#!/usr/bin/env python3
"""
Smart Mail Triage Daemon
========================
Monitors all 7 inboxes every 5 minutes.
When a new unread email is found:
  1. Gemini Pro reads the email content
  2. AI decides which CRM stage it should go to
  3. Moves/creates the CRM lead in the correct stage
  4. Posts a detailed report to Odoo "Mail Reports" channel (ID 188)

Deployed on Mac 32 (192.168.1.32)
Crontab: */5 * * * * (every 5 minutes)
@reboot: auto-start on boot
"""

import sys
import os
import json
import time
import base64
import hashlib
import datetime
import requests
import ssl
import xmlrpc.client
import traceback
import re
import fcntl
import subprocess

sys.path.append("/Users/trung/syncmail-repo-auto")
import gmail_api_client

# ========== CONFIG ==========
ACCOUNTS = [
    "robert@wsoftpro.com",
    "vanessa@wsoftpro.com",
    "supportteam@wsoftpro.com",
    "jennifer@hyperspacedev.com",
    "luna@hyperspacedev.com",
    "helen@interstellarsagency.com",
    "yuna@musubiit.com",
]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

# CRM Stages mapping
CRM_STAGES = {
    "new_lead": {"id": 1, "name": "New"},
    "reply_client": {"id": 3, "name": "Reply Client"},
    "old_lead_followup": {"id": 34, "name": "Old Lead can Follow-up"},
    "proposition": {"id": 5, "name": "Proposition/ Trung Check"},
    "send_email": {"id": 7, "name": "Send Email to Client"},
    "send_email_done": {"id": 35, "name": "Send Email Done"},
    "checking_meeting": {"id": 6, "name": "Checking Meeting"},
    "reject": {"id": 11, "name": "Reject"},
    "mail_bounce": {"id": 18, "name": "Mail bounce"},
    "auto_acknowledge": {"id": 26, "name": "Z - Auto-Acknowledge"},
    "explicit_rejection": {"id": 27, "name": "Z - Explicit Rejection"},
    "recruiter_spam": {"id": 30, "name": "Z - Recruiter Spam"},
    "bounces_errors": {"id": 21, "name": "Z - Bounces & Delivery Errors"},
    "spam_auto_replies": {"id": 22, "name": "Z - Spam & Auto-Replies"},
    "internal_meeting": {"id": 23, "name": "Z - Internal & Meeting Notes"},
    # columns 24 / 20 were deleted from the CRM on 2026-09-10 (merged into Z - Mail Rác);
    # ids kept only as documentation — routing uses JUNK_STAGE_ID below.
    "job_auto_ooo": {"id": 22, "name": "Z - Mail Rác (job auto-reply/OOO)"},
    "job_applications": {"id": 22, "name": "Z - Mail Rác (job application)"},
    "ticket_auto": {"id": 28, "name": "Z - Ticket System Auto-Created"},
    "feedback_survey": {"id": 29, "name": "Z - Feedback/Survey Requests"},
    "closed_resolved": {"id": 31, "name": "Z - Closed/Resolved Tickets"},
}

# ---- Workflow (CRM ticket #462027 "Workflow xử lí email ticket") ----
# Gemini still classifies into the fine-grained keys above (kept for the
# report's "reason"), but the DESTINATION collapses to the workflow's columns:
#   new client, genuine   -> NEW (1)            (manager/AI re-scans NEW daily)
#   new client, junk      -> Z - Mail Rác (22)  (one column for ALL junk)
#   existing client reply -> RESCUE to Reply Client (3) from wherever it sits
NEW_STAGE_ID = 1
REPLY_CLIENT_STAGE_ID = 3
JUNK_STAGE_ID = 22
REPLY_STAGE_ID_FOR_NEW = 3   # Reply Client — where a brand-new lead goes when the mail is a human reply
JUNK_STAGE_KEYS = {
    "recruiter_spam", "spam_auto_replies", "auto_acknowledge",
    "job_applications", "job_auto_ooo", "bounces_errors",
    "ticket_auto", "feedback_survey", "closed_resolved", "internal_meeting",
    # CRITICAL FIX: "explicit_rejection" (Z - Explicit Rejection, id 27) was
    # missing from this set. Gemini's own prompt defines it as a client
    # explicitly saying "not interested" / "unsubscribe" / "take us off your
    # list" — without it here, is_junk was False for that stage_key, so a new
    # sender's explicit rejection landed in New (1) instead of Mail Rác (22),
    # and an EXISTING client's explicit rejection got "rescued" into Reply
    # Client (3) — i.e. treated as if they replied positively — prompting
    # Sales to follow up on someone who just asked to be left alone.
    "explicit_rejection",
}
# "Tag Nguồn Mail" — crm.tag ids, keyed by persona local-part.
# supportteam@ and jennifer@ are intentionally absent: per SKILL.md §2 they
# are scanned inboxes but not reply personas, so tag_for_account() returning
# None for them is by design, not a bug.
MAIL_TAGS = {"robert": 34, "vanessa": 35, "luna": 75, "helen": 76, "yuna": 82}

# ---- Rescue guard (added 2026-09-11 after 21 junk tickets got pulled into Reply Client) ----
# Root cause: GEMINI_API_KEY was empty -> every mail went through fallback_classify(),
# whose default is "new_lead" -> is_junk False -> an EXISTING sender (HubSpot, DMARC,
# mailer-daemon, newsletters...) got "rescued" into Reply Client (3).
# Rule now: only a classification that positively means "a human client wrote back"
# may rescue. A bare "new_lead" is NOT such a signal (it's the fallback default).
HUMAN_REPLY_KEYS = {"reply_client", "proposition", "checking_meeting"}
GEMINI_ACTIVE = bool(GEMINI_API_KEY)
# Senders that are never a client, whatever the classifier says -> always junk.
import re as _re
SYSTEM_SENDER_RE = _re.compile(
    r"(^|[.@-])(no-?reply|noreply|do-?not-?reply|mailer-daemon|postmaster|notifications?|"
    r"alerts?|dmarc|bounce|newsletter|news|marketing|updates?|digest|support@.*\.zendesk\.com|"
    r"gemini-notes|workspace-noreply|calendar-notification)([.@-]|$)",
    _re.IGNORECASE,
)
SYSTEM_SENDER_DOMAINS = (
    "notifications.hubspot.com", "linkedin.com", "todoist.com", "mimecastreport.com",
    "ccsend.com", "sendibm1.com", "mailchimp.com", "sendgrid.net", "constantcontact.com",
    "zendesk.com", "atlassian.net", "slack.com", "clickup.com", "google.com",
)

def is_system_sender(sender_email):
    e = (sender_email or "").lower()
    local, _, domain = e.partition("@")
    if any(domain == d or domain.endswith("." + d) for d in SYSTEM_SENDER_DOMAINS):
        return True
    return bool(SYSTEM_SENDER_RE.search(local)) or bool(SYSTEM_SENDER_RE.search(e))

ODOO_CRM_URL = "https://crm.wsoftpro.com"
ODOO_CRM_DB = "27_05"
ODOO_PAYROLL_URL = "https://payroll.wsoftpro.com"
ODOO_PAYROLL_DB = "29_5"
# Odoo login comes from .env (ODOO_LOGIN / ODOO_PASSWORD) — never hardcode it here (requirement #7).
def _load_env_file(path="/Users/trung/syncmail-repo-auto/.env"):
    """Minimal .env loader so cron/launchd runs work without python-dotenv; never overrides real env."""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass
_load_env_file()
ODOO_LOGIN = os.environ.get("ODOO_LOGIN") or os.environ.get("CRM_USER") or ""
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD") or os.environ.get("CRM_PASSWORD") or ""
if not ODOO_LOGIN or not ODOO_PASSWORD:
    raise SystemExit("smart_mail_daemon: ODOO_LOGIN / ODOO_PASSWORD missing in .env")
MAIL_REPORTS_CHANNEL = 188

PROCESSED_FILE = "/Users/trung/syncmail-repo-auto/.smart_mail_processed.json"

# ========== HELPERS ==========

RETENTION_DAYS = 7  # Gmail query window is only newer_than:2d, so entries
                     # older than that can never be looked up again — keep a
                     # small safety margin rather than growing this file
                     # forever (584 entries / 201KB and climbing as of
                     # 2026-09-10, with zero pruning logic previously).

def prune_processed(data):
    cutoff = datetime.datetime.now() - datetime.timedelta(days=RETENTION_DAYS)
    pruned = {}
    for msg_id, entry in data.items():
        ts = entry.get("time") if isinstance(entry, dict) else None
        if ts:
            try:
                if datetime.datetime.fromisoformat(ts) < cutoff:
                    continue
            except ValueError:
                pass  # unparseable timestamp — keep it, don't risk reprocessing
        pruned[msg_id] = entry
    return pruned

def load_processed():
    try:
        with open(PROCESSED_FILE, "r") as f:
            data = json.load(f)
    except:
        return {}
    return prune_processed(data)

def save_processed(data):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f)

def get_email_body(payload):
    """Extract plain text body from Gmail message payload."""
    body = ""
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                body += base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            elif "parts" in part:
                body += get_email_body(part)
    elif payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        body += base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    return body

def get_email_body_html(payload):
    """Extract the text/html part (for a faithful copy in the lead chatter)."""
    if payload.get("mimeType") == "text/html" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        found = get_email_body_html(part)
        if found:
            return found
    return ""

def clean_html_body(html):
    """Drop the outer <html>/<head>/<body> wrapper — synced emails in the CRM start at inner <div>."""
    if not html:
        return ""
    import re
    html = re.sub(r'(?is)<head>.*?</head>', '', html)
    html = re.sub(r'(?is)</?html[^>]*>', '', html)
    html = re.sub(r'(?is)</?body[^>]*>', '', html)
    return html.strip()

def email_date_to_utc(date_header):
    """RFC-2822 Date header -> 'YYYY-MM-DD HH:MM:SS' in UTC (Odoo's datetime convention)."""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_header).astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def tag_for_account(account):
    local = (account or "").split("@")[0].lower()
    for key, tid in MAIL_TAGS.items():
        if key in local:
            return tid
    return None

def post_email_to_chatter(session, lead_id, sender, subject, body_text, body_html, date_utc=None):
    """Workflow §3: ghi nhận nội dung email của khách vào Chatter.

    Creates the mail.message directly instead of calling crm.lead.message_post():
    message_post() HTML-escapes the body when called over JSON-RPC (body_is_html is
    NOT honoured there — 70 messages rendered as literal '<div …>' text on 2026-09-11),
    while the mail.message Html field only sanitizes. Direct create also lets us keep
    the email's real timestamp (`date`) so the thread reads chronologically."""
    # NOTE: the API user has no 'create' right on mail.message (verified 2026-09-11), but it
    # can message_post() and then WRITE the message. message_post escapes HTML and ignores
    # `date`, so: post (gets the id) -> write the real HTML body + the email's timestamp.
    html = clean_html_body(body_html)
    body = html if html else (body_text or "(no content)")
    # Odoo fetchmail usually posts the same mail into the chatter first (uid 1). If an email
    # message with this exact timestamp already exists on the lead, don't post a duplicate —
    # the caller still does the routing (rescue/tag) based on the classification.
    if date_utc:
        try:
            dup = session.post(f"{ODOO_CRM_URL}/web/dataset/call_kw/mail.message/search_count", json={
                "jsonrpc": "2.0", "method": "call",
                "params": {"model": "mail.message", "method": "search_count",
                           "args": [[["model", "=", "crm.lead"], ["res_id", "=", lead_id],
                                     ["message_type", "=", "email"], ["date", "=", date_utc]]], "kwargs": {}}
            }, timeout=15).json().get("result", 0)
            if dup:
                print(f"  chatter: email dated {date_utc} already on #{lead_id} (fetchmail) — not re-posting")
                return True
        except Exception as e:
            print(f"  chatter dedupe check failed for #{lead_id}: {e} — posting anyway")
    try:
        r = session.post(f"{ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/message_post", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {"model": "crm.lead", "method": "message_post", "args": [[lead_id]],
                       "kwargs": {"body": body, "message_type": "email", "subtype_xmlid": "mail.mt_comment",
                                  "email_from": sender, "subject": subject}}
        }, timeout=20).json()
        if r.get("error"):
            raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:200])
        mid = r.get("result")
        fix = {"body": body}
        if date_utc:
            fix["date"] = date_utc
        if isinstance(mid, int):
            w = session.post(f"{ODOO_CRM_URL}/web/dataset/call_kw/mail.message/write", json={
                "jsonrpc": "2.0", "method": "call",
                "params": {"model": "mail.message", "method": "write", "args": [[mid], fix], "kwargs": {}}
            }, timeout=20).json()
            if w.get("error"):
                print(f"  chatter body/date fix failed for msg {mid}: {str(w['error'])[:120]}")
        else:
            print(f"  message_post returned no id for #{lead_id}: {r!r}"[:160])
        return True
    except Exception as e:
        print(f"  chatter post failed for #{lead_id}: {e}")
        return False

def _legacy_post_email_to_chatter(session, lead_id, sender, subject, body_text, body_html, date_utc):
    """Old message_post-based version (kept for reference; escapes HTML over JSON-RPC)."""
    html = clean_html_body(body_html)
    kwargs = {
        "message_type": "email",
        "subtype_xmlid": "mail.mt_comment",
        "email_from": sender,
        "subject": subject,
    }
    # Guard against pathologically large HTML (deep quote chains / inline
    # base64 images) blowing past nginx/Cloudflare payload limits — a large
    # message_post silently fails today (caught below) and the client's
    # email never makes it into the chatter at all, defeating workflow §3.
    MAX_CHATTER_HTML = 200_000
    if html and len(html) > MAX_CHATTER_HTML:
        html = html[:MAX_CHATTER_HTML] + "<p>… (truncated, see Gmail for full message)</p>"
    if html:
        kwargs["body"] = html
        kwargs["body_is_html"] = True   # without this Odoo escapes the HTML into literal text
    else:
        kwargs["body"] = (body_text or "(no content)")[:MAX_CHATTER_HTML]
    try:
        session.post(f"{ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/message_post", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {"model": "crm.lead", "method": "message_post",
                       "args": [[lead_id]], "kwargs": kwargs}
        }, timeout=20)
        return True
    except Exception as e:
        print(f"  chatter post failed for #{lead_id}: {e}")
        return False

# ---- Antigravity (agy CLI, Gemini Pro) classifier — restored 2026-09-11 ----
# The original design classified mail with Antigravity's Pro model, not a raw Gemini
# API key (GEMINI_API_KEY is empty on Mac 32). `agy -p ... --output-format json` works
# headless for pure text tasks (~15–25 s per call), so: cheap keyword rules first
# (obvious bounces/OOO/auto-acks/rejections never reach agy), then agy for anything
# ambiguous, capped per run so a burst can't blow the 5-minute cron slot.
AGY_BIN = os.path.expanduser("~/.local/bin/agy")
AGY_MODEL = "gemini-3.1-pro-high"
AGY_TIMEOUT_S = 60
AGY_MAX_CALLS_PER_RUN = 24        # ~8–10 min worst case; mail beyond this is DEFERRED to the next run, never keyword-classified
PER_INBOX_NEW_CAP = 8             # max NEW (unprocessed) mails fetched per inbox per run — Gmail lists newest first,
                                  # so fresh replies always go first; the 2-day backlog drains over successive runs
                                  # (added 2026-09-15 with the `newer_than:2d` query: without caps one run fetched
                                  # hundreds of full messages and hit 60 s Gmail timeouts)
AGY_BODY_MAX_CHARS = 15000        # "full content" for the model, with a sanity cap for giant newsletters
DEFER = "DEFER"
_agy_calls = 0
OBVIOUS_JUNK_KEYS = {"bounces_errors", "job_auto_ooo", "auto_acknowledge", "explicit_rejection", "spam_auto_replies"}

def classify_with_agy(sender, subject, body_snippet, account):
    """Ask Antigravity's Pro model. Returns a dict or None (caller falls back)."""
    global _agy_calls
    if _agy_calls >= AGY_MAX_CALLS_PER_RUN or not os.path.exists(AGY_BIN):
        return None
    _agy_calls += 1
    prompt = (
        "You are a strict CRM email triage AI for WSOFTPRO, a Vietnamese software outsourcing company that also "
        "runs recruiting outreach campaigns whose subjects look like '<Role> Job application' — a REPLY to such a "
        "thread is a real client/candidate conversation, not a job application.\n"
        "Classify the email into exactly ONE key:\n"
        "reply_client (client/candidate replying to our thread, needs attention) | proposition (serious inquiry with "
        "project details) | checking_meeting (scheduling/confirming a call) | new_lead (brand-new genuine inquiry "
        "to hire us) | explicit_rejection (not interested / unsubscribe / already have a vendor) | "
        "auto_acknowledge (automated 'we received your message'/ticket acks) | job_auto_ooo (out of office) | "
        "bounces_errors | recruiter_spam (someone selling us services/dev teams/SEO) | spam_auto_replies "
        "(newsletters, promos) | job_applications (job-board alerts, unsolicited CVs) | feedback_survey | "
        "ticket_auto | closed_resolved | internal_meeting.\n"
        f"Inbox: {account}\nFrom: {sender}\nSubject: {subject}\nBody (full):\n{body_snippet[:AGY_BODY_MAX_CHARS]}\n\n"
        'Respond ONLY with JSON: {"stage":"<key>","reason":"<one line>","priority":"high|medium|low"}'
    )
    try:
        out = subprocess.run(
            [AGY_BIN, "-p", prompt, "--model", AGY_MODEL, "--output-format", "json",
             "--print-timeout", f"{AGY_TIMEOUT_S}s", "--disable-slash-commands"],
            capture_output=True, text=True, timeout=AGY_TIMEOUT_S + 15)
        wrapper = json.loads(out.stdout)
        text = (wrapper.get("response") or "").strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        if isinstance(result, dict) and result.get("stage") in CRM_STAGES:
            result["reason"] = f"[agy] {result.get('reason', '')}"
            return result
        print(f"agy: unexpected shape {result!r}")
    except Exception as e:
        print(f"agy classify error: {str(e)[:160]}")
    return None

def classify_with_gemini(sender, subject, body_snippet, account):
    """Classify an email. Policy (trung, 2026-09-11): EVERY mail is read in full by
    Antigravity Pro (agy). Keyword rules are only a fallback when agy itself fails.
    If this run's agy budget is used up, return DEFER so the mail is left unread
    and classified by agy on the next 5-min run instead of being guessed."""
    if not GEMINI_API_KEY:
        if _agy_calls >= AGY_MAX_CALLS_PER_RUN:
            return DEFER
        smart = classify_with_agy(sender, subject, body_snippet, account)
        if smart:
            return smart
        quick = fallback_classify(sender, subject, body_snippet)
        quick["reason"] = f"[keyword fallback — agy failed] {quick['reason']}"
        return quick

    prompt = f"""You are a highly rigorous CRM email triage AI for WSOFTPRO, a software outsourcing company in Vietnam.
Your job is to critically analyze this incoming email and classify it into ONE of these CRM stages. You must be extremely strict about what constitutes a "new_lead".

STAGES:
- "new_lead" = EXTREMELY STRICT CRITERIA. This MUST be a genuine customer explicitly interested in hiring us for software outsourcing, web/app development, or requesting a quote/meeting for a legitimate project. If there is any hint they are selling US something, DO NOT use this stage.
- "reply_client" = Client replying to our email, needs attention
- "proposition" = Serious inquiry with specific project details, needs Trung to check
- "checking_meeting" = Email about scheduling/confirming a meeting or call
- "recruiter_spam" = Strictly filter out people or companies trying to sell us SEO, lead generation, marketing services, recruitment, or other offshore teams offering their services to us.
- "spam_auto_replies" = General spam, newsletters, marketing blasts, or promotional material.
- "auto_acknowledge" = Auto-acknowledgements and automated system replies. Catch phrases like "Thank you for contacting us", "Your request has been received", "Automatische Antwort".
- "explicit_rejection" = Client explicitly rejecting us. Catch phrases like "not interested", "unsubscribe", "take us off your list", "we already have a vendor".
- "job_applications" = Job applications, CVs, job alerts from job boards
- "job_auto_ooo" = Job auto-replies, "Out of office", "OOO" messages.
- "bounces_errors" = Bounce-back, delivery failure notifications
- "internal_meeting" = Internal emails, meeting notes between team
- "ticket_auto" = Automated ticket system notifications (Jira, Zendesk etc.)
- "feedback_survey" = Feedback requests, survey invitations
- "closed_resolved" = Ticket resolved, case closed notifications

EMAIL:
Inbox Account: {account}
From: {sender}
Subject: {subject}
Body (first 500 chars): {body_snippet[:500]}

Respond with ONLY a JSON object:
{{"stage": "<stage_key>", "reason": "<1-line explanation>", "priority": "high|medium|low"}}
"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)
        text = response.text.strip()
        # Extract JSON from response
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)
        # Guard: json.loads() can succeed on non-dict JSON (a bare string,
        # list, or number). Without this check, process_inbox()'s
        # classification.get("stage", ...) call raises AttributeError,
        # which — since it's outside any per-message try/except — aborts
        # the rest of that inbox's batch for the whole run (see the
        # per-message try/except added in process_inbox below).
        if not isinstance(result, dict) or "stage" not in result:
            raise ValueError(f"unexpected Gemini JSON shape: {result!r}")
        return result
    except Exception as e:
        print(f"Gemini API error: {e}")
        return fallback_classify(sender, subject, body_snippet)

def fallback_classify(sender, subject, body_snippet):
    """Rule-based fallback if Gemini API not available."""
    subject_lower = subject.lower()
    sender_lower = sender.lower()
    body_lower = body_snippet.lower()

    # Bounce/delivery errors — check subject, sender, AND body. Previously
    # body_lower/sender_lower were computed but never consulted, so a bounce
    # or auto-reply whose signal is only in the sender address or body (not
    # the subject line) fell through to the "new_lead" default below. If the
    # sender already has a lead, that default (is_junk=False) makes
    # create_or_update_lead() RESCUE the junk into Reply Client — a false
    # positive that only bites during a Gemini outage, when there's no
    # smarter classifier to catch it.
    if (any(kw in subject_lower for kw in ["bounce", "delivery failed", "undeliverable", "failure notice"])
            or any(kw in body_lower for kw in ["delivery failed", "undeliverable", "message could not be delivered", "delivery status notification"])
            or any(sender_lower.startswith(p) for p in ["mailer-daemon@", "postmaster@", "mail delivery"])):
        return {"stage": "bounces_errors", "reason": "Delivery error detected", "priority": "low"}

    # Auto-replies / OOO
    if (any(kw in subject_lower for kw in ["auto-reply", "automatic reply", "out of office", "ooo", "away from"])
            or any(kw in body_lower for kw in ["out of office", "automatic reply", "i am currently away", "i will be out of office"])):
        return {"stage": "job_auto_ooo", "reason": "Auto-reply/OOO detected", "priority": "low"}

    # Automated acknowledgements / ticket-system auto-replies (must run before the
    # reply check: they usually carry a "Re:" prefix and a ticket tag like [T-…]).
    if (any(kw in body_lower for kw in ["thank you for contacting", "we have received your", "your request has been received",
                                         "your message has been received", "has been opened", "a support ticket",
                                         "wir haben ihre", "wir haben deine", "vielen dank für ihre nachricht", "dank für deine nachricht",
                                         "dziękujemy za wiadomość", "nous avons bien reçu"])
            or re.search(r"\[(t|ticket|case|req)[-#: ]?[a-z0-9-]+\]", subject_lower)):
        return {"stage": "auto_acknowledge", "reason": "Automated acknowledgement", "priority": "low"}

    # Explicit rejection ("not interested", "take us off your list", "we
    # already have a vendor") — mirrors Gemini's own "explicit_rejection"
    # category; must be JUNK (see JUNK_STAGE_KEYS fix above) so an existing
    # client who explicitly opted out isn't "rescued" into Reply Client.
    if any(kw in body_lower for kw in ["not interested", "no longer interested", "already have a vendor",
                                        "already working with", "take us off your list", "please remove us"]):
        return {"stage": "explicit_rejection", "reason": "Explicit rejection detected", "priority": "low"}

    # Reply to one of OUR threads (Re:/AW:/RE:/Antw:/SV:/TR:). This MUST come before the
    # "job application" keyword rule: our outreach campaigns are literally titled
    # "<Role> Job application", so every genuine client reply carries that phrase —
    # the old order junked real replies (e.g. "Hi Vanessa, thank you for reaching out…").
    is_reply_subject = bool(re.match(r"^\s*(re|aw|antw|sv|tr|vs|odp)\s*:", subject_lower))
    if is_reply_subject:
        if any(kw in subject_lower for kw in ["meeting", "call", "schedule", "calendar", "introductory"]):
            return {"stage": "checking_meeting", "reason": "Meeting/call discussion", "priority": "high"}
        return {"stage": "reply_client", "reason": "Client reply detected", "priority": "high"}

    # Job applications / job-board alerts (only when it is NOT a reply to our thread)
    if any(kw in subject_lower for kw in ["job application", "job alert", "new position", "career"]):
        return {"stage": "job_applications", "reason": "Job-related email", "priority": "low"}

    # Spam indicators
    if (any(kw in subject_lower for kw in ["unsubscribe", "newsletter", "promotion", "special offer"])
            or any(kw in body_lower for kw in ["unsubscribe", "opt out of future emails"])):
        return {"stage": "spam_auto_replies", "reason": "Marketing/spam detected", "priority": "low"}

    # Meeting/call related
    if any(kw in subject_lower for kw in ["meeting", "call", "schedule", "calendar", "introductory"]):
        return {"stage": "checking_meeting", "reason": "Meeting/call discussion", "priority": "high"}

    # Default: new lead
    return {"stage": "new_lead", "reason": "New incoming email, needs review", "priority": "medium"}

def get_crm_session():
    """Authenticate to CRM and return session.

    Raises on an unsuccessful auth so callers fail fast with a clear error
    instead of getting a session that later throws confusing
    JSONDecodeError("Expecting value...") out of every .json() call — this
    is exactly what /tmp/smart_mail_daemon.log shows happened on 2026-09-10
    ~09:20-09:25 (Odoo returned an empty/non-JSON body, presumably during
    the crm-internal-db-1 port-conflict incident from SKILL.md §8), which
    aborted processing of the rest of that inbox's messages for those runs.
    """
    s = requests.Session()
    s.verify = False
    requests.packages.urllib3.disable_warnings()
    res = s.post(f"{ODOO_CRM_URL}/web/session/authenticate", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {"db": ODOO_CRM_DB, "login": ODOO_LOGIN, "password": ODOO_PASSWORD}
    }, timeout=20)
    try:
        auth_result = res.json()
    except ValueError as e:
        raise RuntimeError(f"CRM auth returned a non-JSON response (status {res.status_code}): {e}")
    if auth_result.get("error") or not (auth_result.get("result") or {}).get("uid"):
        raise RuntimeError(f"CRM auth failed: {auth_result.get('error') or auth_result}")
    return s

def find_existing_lead(session, email_from):
    """Find the client's lead by normalized email — matches regardless of the
    From display-name format (the old strict `email_from =` match created
    duplicate tickets), and includes archived leads so a reply can rescue them."""
    addr = (email_from or "").strip().lower()
    fields = ["id", "name", "stage_id", "email_from", "active", "type", "write_date", "tag_ids"]
    for domain in ([["email_normalized", "=", addr]], [["email_from", "ilike", addr]]):
        res = session.post(f"{ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/search_read", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "crm.lead", "method": "search_read",
                "args": [domain],
                "kwargs": {"fields": fields, "limit": 10, "order": "write_date desc",
                           "context": {"active_test": False}}
            }
        })
        results = res.json().get("result", [])
        if results:
            # Clients often have duplicate tickets (see #461396/#461397, #460966/#461772).
            # Rescue the one that is actually being worked, not a stray copy:
            #   1) opportunity over lead, 2) not sitting in New / Mail Rác,
            #   3) active over archived, 4) most recently updated.
            def rank(r):
                stage_id = (r.get("stage_id") or [None])[0]
                return (
                    1 if r.get("type") == "opportunity" else 0,
                    0 if stage_id in (NEW_STAGE_ID, JUNK_STAGE_ID) else 1,
                    1 if r.get("active") else 0,
                    r.get("write_date") or "",
                )
            return max(results, key=rank)
    return None

def create_or_update_lead(session, sender, subject, body_text, body_html, is_junk, account, is_reply=False, date_utc=None):
    """Workflow (CRM ticket #462027):
      - New client            -> NEW (1), or Z - Mail Rác (22) if junk. Auto-tag "Mail <persona>".
      - Existing client reply -> log the email in the chatter and RESCUE it to
                                 Reply Client (3) from wherever it sits (Send Email
                                 Done, Follow Up 1/2, even Mail Rác or archived).
      - Existing client, junk -> log only (OOO / auto-ack / bounce), keep the stage —
                                 never throw a live client ticket into the junk column.
    Returns (lead_id, action, stage_name)."""
    email_from = sender
    if "<" in sender and ">" in sender:
        email_from = sender.split("<")[1].split(">")[0]
    email_from = email_from.strip().lower()
    contact_name = sender.split("<")[0].strip().strip('"') if "<" in sender else sender.split("@")[0]
    tag_id = tag_for_account(account)

    existing = find_existing_lead(session, email_from)

    if existing:
        lead_id = existing["id"]
        post_email_to_chatter(session, lead_id, sender, subject, body_text, body_html, date_utc)
        vals = {}
        # One source tag per ticket (trung, 2026-09-11): only tag if the lead has no Mail tag yet.
        # Auto-responders reply into every persona inbox; appending a tag per inbox produced
        # 2–5 tags on the same ticket.
        if tag_id and not (set(existing.get("tag_ids") or []) & set(MAIL_TAGS.values())):
            vals["tag_ids"] = [(4, tag_id)]
        if is_junk:
            # Spec (§3): "log to chatter only, stage untouched — never throw
            # a live client ticket into the junk column." That also means
            # don't resurrect an archived/dead lead just because it received
            # more junk (an OOO/bounce/auto-ack on an old, closed deal) — the
            # un-archive below only happens on the genuine-reply RESCUE path.
            stage_field = existing.get("stage_id") or []
            stage_name = stage_field[1] if len(stage_field) > 1 else "?"
            action = "logged (junk, stage kept)"
        elif not is_reply:
            # Not junk, but not a confident "client replied" signal either (e.g. the
            # fallback default new_lead). Log it, keep the stage — never rescue on a guess.
            stage_field = existing.get("stage_id") or []
            stage_name = stage_field[1] if len(stage_field) > 1 else "?"
            action = "logged (unclassified, stage kept)"
        else:
            if existing.get("active") is False:
                vals["active"] = True
            vals["stage_id"] = REPLY_CLIENT_STAGE_ID
            # The team works in the Pipeline Kanban, which only shows type='opportunity'.
            # A rescued lead that stays type='lead' is invisible there (2026-09-11: 8 of 15
            # tickets in Reply Client were hidden this way) — so a client reply also converts.
            vals["type"] = "opportunity"
            stage_name = "Reply Client"
            action = "rescued → Reply Client"
        if vals:
            session.post(f"{ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/write", json={
                "jsonrpc": "2.0", "method": "call",
                "params": {"model": "crm.lead", "method": "write",
                           "args": [[lead_id], vals], "kwargs": {}}
            })
        return lead_id, action, stage_name

    # 2026-09-22: a mail agy classified as a genuine human reply (is_reply) but whose sender has no
    # ticket yet (a reply to a cold outreach — FidesIQ #463418) used to be created in New, where
    # nobody looked for it; trung: "ticket này đâu, không thấy trong cột Reply Client". A reply
    # IS the team's work queue, so create it straight in Reply Client. Plain new inquiries stay in New.
    stage_id = JUNK_STAGE_ID if is_junk else (REPLY_STAGE_ID_FOR_NEW if is_reply else NEW_STAGE_ID)
    stage_name = "Z - Mail Rác" if is_junk else "New"
    lead_vals = {
        "name": subject[:100],
        "email_from": email_from,
        "contact_name": contact_name,
        "description": (body_text or "")[:1000],
        "stage_id": stage_id,
        # genuine new mail must be visible in the Pipeline Kanban's New column -> opportunity;
        # junk can stay a plain lead (Mail Rác is folded anyway)
        "type": "lead" if is_junk else "opportunity",
    }
    if tag_id:
        lead_vals["tag_ids"] = [(4, tag_id)]
    res = session.post(f"{ODOO_CRM_URL}/web/dataset/call_kw/crm.lead/create", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {"model": "crm.lead", "method": "create",
                   "args": [lead_vals], "kwargs": {}}
    })
    lead_id = res.json().get("result")
    if lead_id:
        post_email_to_chatter(session, lead_id, sender, subject, body_text, body_html, date_utc)
    return lead_id, "created", stage_name

def post_to_mail_reports(message):
    """Post message to Odoo Mail Reports channel."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_PAYROLL_URL}/xmlrpc/2/common", context=ctx)
        uid = common.authenticate(ODOO_PAYROLL_DB, ODOO_LOGIN, ODOO_PASSWORD, {})
        models = xmlrpc.client.ServerProxy(f"{ODOO_PAYROLL_URL}/xmlrpc/2/object", context=ctx)
        try:
            models.execute_kw(ODOO_PAYROLL_DB, uid, ODOO_PASSWORD, "mail.channel", "message_post",
                [MAIL_REPORTS_CHANNEL],
                {"body": message, "message_type": "comment", "subtype_xmlid": "mail.mt_comment"}
            )
        except xmlrpc.client.Fault as e:
            if 'cannot marshal <class' not in str(e):
                raise e
        print("Report posted to Mail Reports.")
    except Exception as e:
        print(f"Failed to post report: {e}")

def process_inbox(account, processed):
    """Check one inbox for new unread emails and process them."""
    results = []
    try:
        service = gmail_api_client.get_gmail_service(account)
        # Fetch ALL unread mail from the last 2 days (paginated). The previous
        # "newer_than:1h" + maxResults=10 combination silently dropped mail whenever
        # >10 unread piled up — processed mail is never marked read, so it kept
        # occupying the 10 slots until the unprocessed ones aged out of the window.
        # 2026-09-15: dropped `is:unread`. Odoo's own fetchmail (IMAP) marks mails SEEN within
        # minutes of arrival, so an unread-only query silently skipped every client reply that
        # fetchmail (or a human) opened first (#462037: two VESLOG replies never seen by this
        # daemon, ticket stuck in Send Email Done). Dedupe relies on the processed-id file instead.
        # SMART_MAIL_QUERY lets a one-off run widen the window (e.g. "newer_than:4d" after an outage);
        # the processed-id cache makes re-scanning safe. Default unchanged.
        query = os.environ.get("SMART_MAIL_QUERY", "newer_than:2d")
        # 2026-09-21: the listing below stops at 300 messages, and vanessa@ alone receives 300+
        # mails a DAY, most of them from our own domains (skipped below as internal_skip anyway).
        # With them in the list, "300 newest" reached back only ~20 h, so during the 3-day outage
        # catch-up 82 external mails older than that were never even listed. Exclude our own
        # senders in the query itself so the 300 slots hold only mail we would actually process.
        query += " -from:wsoftpro.com -from:hyperspacedev.com -from:interstellarsagency.com -from:musubiit.com"
        messages = []
        page_token = None
        while True:
            response = service.users().messages().list(
                userId="me", q=query, maxResults=100, pageToken=page_token).execute()
            messages.extend(response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token or len(messages) >= 300:
                break
        
        if not messages:
            return results
        
        crm_session = get_crm_session()
        new_fetched = 0

        for msg_meta in messages:
            msg_id = msg_meta["id"]

            # Skip if already processed
            if msg_id in processed:
                continue

            # Budget guards BEFORE the expensive full fetch: once this run can no longer
            # classify (agy budget spent) or this inbox has had its share, leave the rest
            # unprocessed for the next 5-minute cycle instead of fetching it for nothing.
            if _agy_calls >= AGY_MAX_CALLS_PER_RUN:
                print(f"  agy budget exhausted — leaving the rest of {account} for the next run")
                break
            if new_fetched >= PER_INBOX_NEW_CAP:
                print(f"  per-inbox cap {PER_INBOX_NEW_CAP} reached — leaving the rest of {account} for the next run")
                break
            new_fetched += 1

            # Per-message guard: previously the ONLY try/except wrapping this
            # work was the one around the whole account loop (below), so one
            # bad message (e.g. a transient CRM auth/JSON error — confirmed
            # in /tmp/smart_mail_daemon.log at 2026-09-10 09:20-09:25) aborted
            # every remaining message in this inbox for the run. Catch here
            # instead so one failure only skips that one message (it stays
            # unprocessed and is retried on the next 5-min run) rather than
            # blocking the whole inbox.
            try:
                # Get full message
                msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}

                sender = headers.get("From", "unknown")
                subject = headers.get("Subject", "(no subject)")
                date = headers.get("Date", "")
                body = get_email_body(msg["payload"])
                body_html = get_email_body_html(msg["payload"])
                snippet = msg.get("snippet", "")

                # Skip internal emails between our own accounts
                sender_email = sender.lower()
                if "<" in sender_email:
                    sender_email = sender_email.split("<")[1].split(">")[0]
                our_domains = ["wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com", "musubiit.com"]
                if any(sender_email.endswith(d) for d in our_domains):
                    processed[msg_id] = {"stage": "internal_skip", "time": datetime.datetime.now().isoformat()}
                    continue

                # Classify with Gemini AI
                # Full content for the model: plain-text part, else HTML stripped to text, else snippet
                full_text = body or re.sub(r"<[^>]+>", " ", body_html or "") or snippet
                full_text = re.sub(r"[ \t]+", " ", full_text).strip()
                classification = classify_with_gemini(sender, subject, full_text, account)
                if classification == DEFER:
                    print(f"  agy budget for this run exhausted — deferring {msg_id} to next run")
                    continue          # not marked processed -> picked up next cycle
                stage_key = classification.get("stage", "new_lead")
                reason = classification.get("reason", "No reason")
                priority = str(classification.get("priority", "medium")).lower()
                is_junk = stage_key in JUNK_STAGE_KEYS
                if is_system_sender(sender_email):
                    is_junk = True
                    reason = f"system/notification sender ({sender_email}) — {reason}"
                # Only a positive "client wrote back" signal may rescue a ticket.
                # "new_lead" is the fallback default (and Gemini's catch-all), so it
                # never rescues on its own; without an API key nothing rescues except
                # a clear "Re:" reply (fallback maps that to reply_client).
                is_reply = (not is_junk) and stage_key in HUMAN_REPLY_KEYS

                # Destination column is decided inside create_or_update_lead (workflow #462027)

                # Create / rescue / log the CRM lead
                lead_id, action, stage_name = create_or_update_lead(
                    crm_session, sender, subject, body if body else snippet, body_html, is_junk, account,
                    is_reply=is_reply, date_utc=email_date_to_utc(date))

                # Mark as processed
                processed[msg_id] = {
                    "stage": stage_key,
                    "lead_id": lead_id,
                    "action": action,
                    "snippet": (body[:200] if body else snippet[:200]),
                    "time": datetime.datetime.now().isoformat()
                }

                # Build result
                priority_icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(priority, "⚪")
                results.append({
                    "account": account,
                    "sender": sender,
                    "subject": subject,
                    "date": date,
                    "stage_name": stage_name,
                    "stage_key": stage_key,
                    "is_junk": is_junk,
                    "reason": reason,
                    "priority": priority,
                    "priority_icon": priority_icon,
                    "lead_id": lead_id,
                    "action": action,
                    "snippet": (body[:200] if body else snippet[:200]),
                })
            except Exception as e:
                print(f"  Error processing message {msg_id} in {account}: {e}")
                traceback.print_exc()
                continue

    except Exception as e:
        print(f"Error processing {account}: {e}")
        traceback.print_exc()

    return results

# ========== MAIN ==========
def main():
    now = datetime.datetime.now()
    print(f"=== Smart Mail Daemon running at {now} ===")
    # Single-instance lock: agy calls can take a while; never let two cron runs overlap
    # (they'd both process the same unread mail and create duplicate tickets).
    lock = open("/tmp/smart_mail_daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Previous run still in progress — skipping this cycle.")
        return
    
    processed = load_processed()
    all_results = []
    
    for account in ACCOUNTS:
        results = process_inbox(account, processed)
        all_results.extend(results)
    
    save_processed(processed)
    
    if not all_results:
        print("No new external emails found.")
        return
    
    # Build HTML report for Odoo.
    # These four buckets are mutually exclusive AND exhaustive over
    # all_results (every row is exactly one of: new+genuine, new+junk,
    # rescued, or existing+junk-logged) — n_created/n_rescued/n_junk used to
    # be neither: a newly-created JUNK lead had action=='created' (counted
    # as "ticket mới (New)") AND stage_name starting with 'Z -' (also
    # counted as junk) — double-counted and mislabeled as New when it
    # actually went straight to Mail Rác — while an EXISTING client's junk
    # mail (action == 'logged (junk, stage kept)', stage_name left as
    # whatever stage the lead already sat in, e.g. "Send Email Done") fell
    # into none of the three buckets and silently vanished from the summary.
    n_created = sum(1 for r in all_results if r['action'] == 'created' and not r.get('is_junk'))
    n_created_junk = sum(1 for r in all_results if r['action'] == 'created' and r.get('is_junk'))
    n_rescued = sum(1 for r in all_results if str(r['action']).startswith('rescued'))
    n_junk_logged = sum(1 for r in all_results if r['action'] == 'logged (junk, stage kept)')
    n_junk = n_created_junk + n_junk_logged  # total junk classifications this run
    # Standard Bot Report format: 🤖 title / ✅ Kết quả / 📊 Chi tiết / 👉 Hành động tiếp theo / 📍 Truy cập
    report = f"<p>🤖 <b>Bot Report: 📬 AI Mail Triage ({now.strftime('%H:%M %d/%m')})</b></p>\n"
    report += (f"<p>✅ <b>Kết quả:</b> {len(all_results)} email mới được xử lý — "
               f"{n_created} ticket mới (New), {n_rescued} khách reply (rescue → Reply Client), "
               f"{n_created_junk} vào Z - Mail Rác"
               + (f", {n_junk_logged} rác từ khách cũ (giữ nguyên stage)" if n_junk_logged else "")
               + "</p>\n")
    report += "<p>📊 <b>Chi tiết:</b></p>\n"
    
    has_high = False
    for r in all_results:
        if str(r['priority']).lower() == 'high':   # Gemini returns lowercase; 'HIGH' never matched
            has_high = True
            
        lead_link = f"<a href='{ODOO_CRM_URL}/web#id={r['lead_id']}&model=crm.lead&view_type=form'>#{r['lead_id']}</a>" if r['lead_id'] else "N/A"
        icon = r.get('priority_icon', '⚪')
        report += f"<p>{icon} <b>[{r['priority']}]</b> Inbox: {r['account'].split('@')[0]}</p>\n"
        report += f"<p>📧 From: {r['sender']}</p>\n"
        report += f"<p>📝 Subject: {r['subject']}</p>\n"
        snippet_text = r.get('snippet', '')[:200]
        report += f"<p>💬 Nội dung: <i>{snippet_text}</i></p>\n"
        # Show what the AI decided; for "logged (… stage kept)" the lead's column did not
        # change, so say so explicitly instead of printing the old column as if it were the decision.
        classified = CRM_STAGES.get(r.get('stage_key'), {}).get('name', r['stage_name'])
        decision = classified if r['action'] in ('created',) or str(r['action']).startswith('rescued') \
            else f"{classified} (giữ nguyên cột: {r['stage_name']})"
        report += f"<p>🏷️ AI Decision: <b>{decision}</b> — {r['reason']}</p>\n"
        report += f"<p>🔗 Lead: {lead_link} ({r['action']})</p>\n"
        report += "<p>---</p>\n"
    
    next_action = []
    if n_rescued:
        next_action.append(f"{n_rescued} ticket khách vừa reply đang chờ ở Reply Client — Sales vào trả lời")
    if has_high:
        next_action.append("Lead 🔴 HIGH cần được xử lý trong ngày")
    if n_created:
        next_action.append(f"{n_created} ticket mới ở cột New — Quản lý quét lại rác rồi kéo sang Reply Client")
    report += (f"<p>👉 <b>Hành động tiếp theo:</b> "
               f"{'; '.join(next_action) if next_action else 'Không cần làm gì'}</p>\n")
    report += f"<p>📍 <b>Truy cập:</b> <a href='{ODOO_CRM_URL}/odoo/crm'>CRM Pipeline</a></p>\n"
    
    post_to_mail_reports(report)
    print(f"Processed {len(all_results)} emails, report posted.")

if __name__ == "__main__":
    main()
