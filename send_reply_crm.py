#!/usr/bin/env python3
"""send_reply_crm.py — Auto-send reply emails from CRM and move to Follow-up. (GMAIL API VERSION)

Flow:
1. Fetch all CRM leads in 'Send Email to Client' stage with reply_email filled
2. For each lead: 
   - Extract thread ID from lead name
   - Use Gmail API to send a reply
   - Move the CRM lead to 'Enrich/Follow-up/ Other' stage
3. Uses WSoftPro/Interstellars JSON credentials.
"""
import json
import time
import os
import sys
import re
import glob
import logging
import requests
from datetime import datetime

import gmail_api_client

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EMAILS_DIR = os.path.join(SCRIPT_DIR, 'emails')
LOGS_DIR = os.path.join(SCRIPT_DIR, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

# Setup logging — both console and file
log_file = os.path.join(LOGS_DIR, 'send_reply.log')
logger = logging.getLogger('send_reply')
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(log_file, encoding='utf-8')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(ch)

def log(msg, level='info'):
    getattr(logger, level)(msg)
    if level == 'info':
        print(msg, flush=True)

def load_env():
    env_path = os.path.join(SCRIPT_DIR, '.env')
    _env = {}
    if not os.path.exists(env_path):
        print(f"⚠️  No .env file found at {env_path}")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                _env[key.strip()] = val.strip()
    return _env

ENV = load_env()

CRM_URL = ENV.get("CRM_URL", "https://crm.wsoftpro.com")
CRM_USER = ENV.get("CRM_USER", "")
CRM_PASS = ENV.get("CRM_PASSWORD", "")
CRM_DB = ENV.get("CRM_DB", "")

GMAIL_ACCOUNTS = {}
for key_prefix in ['ROBERT', 'VANESSA', 'LUNA', 'HELEN']:
    email = ENV.get(f'GMAIL_{key_prefix}_EMAIL', '')
    name = ENV.get(f'GMAIL_{key_prefix}_NAME', key_prefix.capitalize())
    if email:
        GMAIL_ACCOUNTS[email] = {
            'name': name,
            'email': email,
            'key': key_prefix.lower(),
        }

DEFAULT_SENDER = 'vanessa@wsoftpro.com'

STAGE_SEND_EMAIL = 7
STAGE_FOLLOWUP = 35
STAGE_REPLY_CLIENT = 3
STAGE_FOLLOWUP_X1 = 9
STAGE_DONE_FOLLOWUP = 10
STAGE_TRUNG_CHECK = 5
STAGE_OLD_LEAD_FOLLOWUP = 34   # "Old Lead cần Follow-up" — workflow #462027 §2 (Bot Cảnh Sát)

DRY_RUN = "--send" not in sys.argv and "--auto" not in sys.argv
AUTO_MODE = "--auto" in sys.argv

def detect_sender_from_reply(reply_html):
    if not reply_html:
        return DEFAULT_SENDER
    reply_lower = reply_html.lower()
    for email, acct in GMAIL_ACCOUNTS.items():
        if email.lower() in reply_lower:
            return email
    for email, acct in GMAIL_ACCOUNTS.items():
        domain = email.split('@')[-1]
        if domain.lower() in reply_lower and domain != 'wsoftpro.com':
            return email
    return None

def detect_sender_from_name_mention(text):
    """Catch cases like the client writing 'Hello Helen' — a bare first-name
    mention with no email/domain, which detect_sender_from_reply misses.
    Bug found on lead #434380: client only ever addressed 'Helen', so both
    the email/domain check above AND the user_id check failed, and the
    script silently fell back to DEFAULT_SENDER (Vanessa), confusing the
    client mid-thread ("Who is Vanessa Ha?")."""
    if not text:
        return None
    plain = re.sub(r'<[^>]+>', ' ', text)
    for email, acct in GMAIL_ACCOUNTS.items():
        name = acct.get('name', '')
        if name and re.search(r'\b' + re.escape(name) + r'\b', plain, re.IGNORECASE):
            return email
    return None

def detect_sender_from_json(subject, recipient_email):
    clean_subj = re.sub(r'^(Re:\s*|Fwd:\s*|\[.*?\]\s*)*', '', subject, flags=re.IGNORECASE).strip().lower()
    recipient_lower = recipient_email.lower() if recipient_email else ''
    recipient_domain = recipient_lower.split('@')[-1] if '@' in recipient_lower else ''
    for json_file in glob.glob(os.path.join(EMAILS_DIR, '*.json')):
        try:
            fname = os.path.basename(json_file).lower()
            if not fname.startswith('gmail_'):
                continue
            with open(json_file) as f:
                data = json.load(f)
            account_email = data.get('account', '')
            if not account_email:
                continue
            for em in data.get('emails', []):
                email_subj = (em.get('subject', '') or '').lower()
                email_to = (em.get('to', '') or '').lower()
                email_from = (em.get('from', '') or '').lower()
                is_from_client = (recipient_lower in email_from or (recipient_domain and recipient_domain in email_from))
                is_to_client = (recipient_lower in email_to or (recipient_domain and recipient_domain in email_to))
                if not is_from_client and not is_to_client:
                    continue
                clean_email_subj = re.sub(r'^(re:\s*|fwd:\s*|\[.*?\]\s*)*', '', email_subj, flags=re.IGNORECASE).strip()
                if clean_subj and clean_email_subj and (clean_subj in clean_email_subj or clean_email_subj in clean_subj):
                    if account_email in GMAIL_ACCOUNTS:
                        return account_email
        except:
            pass
    return None

USER_TO_GMAIL = {}

def load_user_mapping():
    global USER_TO_GMAIL
    try:
        session = crm_session()
        res = session.post(f"{CRM_URL}/web/dataset/call_kw/res.users/search_read", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "res.users", "method": "search_read",
                "args": [[]],
                "kwargs": {"fields": ["name", "email"], "limit": 50}
            }
        })
        for user in res.json().get("result", []):
            odoo_email = user.get("email", "")
            user_id = user.get("id")
            if odoo_email in GMAIL_ACCOUNTS:
                USER_TO_GMAIL[user_id] = odoo_email
        logger.debug(f"Loaded {len(USER_TO_GMAIL)} user→Gmail mappings")
    except Exception as e:
        logger.warning(f"Failed to load user mapping: {e}")

def detect_sender_from_user(user_id_field):
    if not user_id_field:
        return None
    uid = user_id_field[0] if isinstance(user_id_field, (list, tuple)) else user_id_field
    return USER_TO_GMAIL.get(uid)

def get_sender_info(sender_email):
    if sender_email in GMAIL_ACCOUNTS:
        acct = GMAIL_ACCOUNTS[sender_email]
        return acct['name'], acct['email']
    return 'Vanessa', DEFAULT_SENDER

_ERROR_STAGE_ID = None
def get_error_stage(session):
    global _ERROR_STAGE_ID
    if _ERROR_STAGE_ID is not None:
        return _ERROR_STAGE_ID
    try:
        res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.stage/search_read", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "crm.stage", "method": "search_read",
                "args": [[["name", "=", "Unable to Send Email"]]],
                "kwargs": {"fields": ["id", "name"], "limit": 1}
            }
        }, timeout=5)
        stages = res.json().get("result", [])
        if stages:
            _ERROR_STAGE_ID = stages[0]["id"]
            return _ERROR_STAGE_ID
    except Exception as e:
        logger.warning(f"Could not fetch error stage dynamically: {e}")
    return STAGE_TRUNG_CHECK

SIGNATURES = {}

def load_odoo_signatures():
    global SIGNATURES
    try:
        session = crm_session()
        res = session.post(f"{CRM_URL}/web/dataset/call_kw/res.users/search_read", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "res.users", "method": "search_read",
                "args": [[["email", "in", list(GMAIL_ACCOUNTS.keys())]]],
                "kwargs": {"fields": ["name", "email", "signature"], "limit": 20}
            }
        })
        for user in res.json().get("result", []):
            email = user.get("email", "")
            sig = user.get("signature", "")
            if email and sig:
                SIGNATURES[email] = sig
        logger.debug(f"Loaded {len(SIGNATURES)} signatures from Odoo")
    except Exception as e:
        logger.warning(f"Failed to load Odoo signatures: {e}")

def append_signature(reply_html, sender_email):
    if not reply_html or not sender_email:
        return reply_html
    sig_html = SIGNATURES.get(sender_email, '')
    if not sig_html:
        return reply_html
    reply_lower = reply_html.lower()
    if sender_email.lower() in reply_lower:
        return reply_html
    acct = GMAIL_ACCOUNTS.get(sender_email, {})
    acct_name = acct.get('name', '')
    if acct_name and acct_name.lower() in reply_lower:
        return reply_html
    reply_plain = re.sub(r'<[^>]+>', ' ', reply_html).lower()
    reply_plain = re.sub(r'&nbsp;', ' ', reply_plain)
    if acct_name and acct_name.lower() in reply_plain:
        return reply_html
    return reply_html + '<br><div>--</div>' + sig_html

def crm_session():
    session = requests.Session()
    res = session.post(f"{CRM_URL}/web/session/authenticate", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {"db": CRM_DB, "login": CRM_USER, "password": CRM_PASS}
    })
    if "error" in res.json():
        print(f"❌ CRM auth failed")
        sys.exit(1)
    return session

def fetch_tickets_to_send(session):
    res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/search_read", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {
            "model": "crm.lead", "method": "search_read",
            "args": [[[
                "active", "=", True
            ], [
                "stage_id", "=", STAGE_SEND_EMAIL
            ], "|", "|",
                ["reply_email", "!=", False],
                ["follow_up_x1", "!=", False],
                ["follow_up_x2", "!=", False]
            ]],
            "kwargs": {
                "fields": ["name", "email_from", "reply_email", "follow_up_x1", "follow_up_x2",
                           "message_id", "partner_name", "user_id"],
                "order": "id asc"
            }
        }
    })
    return res.json().get("result", [])

def fetch_first_inbound_body(session, lead_id):
    """Fetch the client's earliest inbound email body on this lead, so name
    mentions like 'Hello Helen' can be checked even when the outgoing draft
    itself doesn't repeat that name."""
    try:
        res = session.post(f"{CRM_URL}/web/dataset/call_kw/mail.message/search_read", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "mail.message", "method": "search_read",
                "args": [[["model", "=", "crm.lead"], ["res_id", "=", lead_id], ["message_type", "=", "email"]]],
                "kwargs": {"fields": ["body"], "order": "date asc", "limit": 1}
            }
        }, timeout=10)
        results = res.json().get("result", [])
        if results:
            return results[0].get("body") or ""
    except Exception as e:
        logger.debug(f"Could not fetch inbound body for lead {lead_id}: {e}")
    return ""

def post_sender_warning(session, lead_id, used_sender):
    try:
        session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/message_post", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "crm.lead", "method": "message_post",
                "args": [[lead_id]],
                "kwargs": {
                    "body": (f"⚠️ Auto-reply: không xác định được persona gốc (Helen/Luna/Robert/Vanessa) "
                             f"khách đã từng liên hệ — đã gửi tạm bằng {used_sender}. "
                             f"Vui lòng kiểm tra lại đúng người khách từng làm việc cùng."),
                    "message_type": "comment",
                }
            }
        }, timeout=10)
    except Exception as e:
        logger.debug(f"Could not post sender warning note for lead {lead_id}: {e}")

def move_to_stage(session, lead_id, stage_id):
    res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/write", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {
            "model": "crm.lead", "method": "write",
            "args": [[lead_id], {"stage_id": stage_id}],
            "kwargs": {}
        }
    })
    return "error" not in res.json()

def move_to_followup(session, lead_id):
    return move_to_stage(session, lead_id, STAGE_FOLLOWUP)

def clear_email_field(session, lead_id, field_name):
    try:
        session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/write", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "crm.lead", "method": "write",
                "args": [[lead_id], {field_name: False}],
                "kwargs": {}
            }
        })
    except:
        pass

def is_field_filled(html_str):
    if not html_str: return False
    clean = re.sub(r'<[^>]+>', '', html_str).strip()
    if clean: return True
    if '<img' in html_str.lower(): return True
    if '<a ' in html_str.lower(): return True
    return False

def detect_email_field(ticket):
    reply = ticket.get('reply_email') or ''
    fu1 = ticket.get('follow_up_x1') or ''
    fu2 = ticket.get('follow_up_x2') or ''
    
    if is_field_filled(reply):
        return ('reply_email', reply, STAGE_FOLLOWUP, 'Send Email Done')
    elif is_field_filled(fu1):
        return ('follow_up_x1', fu1, STAGE_FOLLOWUP_X1, 'Done Follow Up 1')
    elif is_field_filled(fu2):
        return ('follow_up_x2', fu2, STAGE_DONE_FOLLOWUP, 'Done Follow Up 2')
    else:
        return (None, None, None, None)

def check_followup_x1_stale(session):
    # 2026-09-15 (trung): this 10-minute bot moved silent tickets to "Old Lead cần Follow-up" (34),
    # a column nobody worked. Replaced by followup_reminder.py (daily 08:00 VN, launchd
    # com.syncmail.followup_reminder) which moves them to Reply Client (3) instead. Kept the
    # function for reference; disabled so the two bots don't fight over the same tickets.
    return 0
    from datetime import timedelta
    # Odoo stores/queries Datetime fields (date_last_stage_update) in UTC; a
    # naive "YYYY-MM-DD HH:MM:SS" string in a search domain is interpreted as
    # UTC by the ORM. This machine's local time is Asia/Ho_Chi_Minh (UTC+7),
    # so building the threshold from datetime.now() shifted the effective
    # "3 days" cutoff by ~7 hours. Use UTC to match Odoo's own convention.
    three_days_ago = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/search_read", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {
            "model": "crm.lead", "method": "search_read",
            "args": [[[
                "stage_id", "in", [STAGE_FOLLOWUP, STAGE_FOLLOWUP_X1]
            ], [
                "active", "=", True
            ], [
                "date_last_stage_update", "<=", three_days_ago
            ]]],
            "kwargs": {
                "fields": ["id", "name", "date_last_stage_update"],
                "order": "id asc",
                "limit": 50
            }
        }
    })
    stale_tickets = res.json().get("result", [])
    moved_count = 0
    for t in stale_tickets:
        tid = t['id']
        tname = t.get('name', '')
        logger.info(f'   ⏰ #{tid} {tname[:40]} — no client reply > 3 days, moving to Old Lead cần Follow-up')
        if move_to_stage(session, tid, STAGE_OLD_LEAD_FOLLOWUP):
            try:
                session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/message_post", json={
                    "jsonrpc": "2.0", "method": "call",
                    "params": {
                        "model": "crm.lead", "method": "message_post",
                        "args": [[tid]],
                        "kwargs": {
                            "body": ("⏰ Khách chưa reply sau 3 ngày → chuyển về Old Lead cần Follow-up. "
                                     "Điền follow_up_x1 (hoặc follow_up_x2 nếu đã chăm sóc lần 1) rồi kéo ticket sang Send Email to Client."),
                            "message_type": "comment",
                            "subtype_xmlid": "mail.mt_note",
                        }
                    }
                })
            except:
                pass
            moved_count += 1
    return moved_count

def post_ticket_comment(session, lead_id, to_email, reply_html, sent_time=None):
    if not sent_time:
        sent_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plain_text = re.sub(r'<br\s*/?>', '\n', reply_html)
    plain_text = re.sub(r'</(p|div|tr|li)>', '\n', plain_text)
    plain_text = re.sub(r'<[^>]+>', '', plain_text)
    plain_text = re.sub(r'&nbsp;', ' ', plain_text)
    plain_text = re.sub(r'&amp;', '&', plain_text)
    plain_text = re.sub(r'&lt;', '<', plain_text)
    plain_text = re.sub(r'&gt;', '>', plain_text)
    plain_text = re.sub(r'\n{3,}', '\n\n', plain_text)
    plain_text = plain_text.strip()
    if len(plain_text) > 500:
        plain_text = plain_text[:500] + '...'
    comment_body = f"📧 Email sent to {to_email} at {sent_time}\n\n--- Message ---\n{plain_text}"
    try:
        res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/message_post", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "crm.lead", "method": "message_post",
                "args": [[lead_id]],
                "kwargs": {
                    "body": comment_body,
                    "message_type": "comment",
                    "subtype_xmlid": "mail.mt_note",
                }
            }
        })
        result = res.json()
        if result.get('result'):
            return True
        logger.debug(f'Comment post response: {json.dumps(result)[:200]}')
        return False
    except Exception as e:
        logger.error(f'Failed to post comment: {e}')
        return False

def extract_thread_id(name):
    match = re.search(r'thread::([^\s\]]+)', name or '')
    if match:
        tid = match.group(1).rstrip(':').strip()
        return tid
    return None

def extract_recipient_email(email_from):
    match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', email_from or '')
    return match.group(0) if match else ''

def clean_reply_html(html_body):
    if not html_body:
        return ''
    html_body = re.sub(r'\s*data-oe-[a-z-]+="[^"]*"', '', html_body)
    return html_body

def find_threads_in_json(subject, recipient_email):
    clean_subj = re.sub(r'^(Re:\s*|Fwd:\s*|\[.*?\]\s*)*', '', subject, flags=re.IGNORECASE).strip().lower()
    recipient_lower = recipient_email.lower() if recipient_email else ''
    recipient_domain = recipient_lower.split('@')[-1] if '@' in recipient_lower else ''
    matches = []
    for json_file in glob.glob(os.path.join(EMAILS_DIR, '*.json')):
        try:
            fname = os.path.basename(json_file).lower()
            if not fname.startswith('gmail_'):
                continue
            with open(json_file) as f:
                data = json.load(f)
            account_email = data.get('account', '')
            if not account_email or account_email not in GMAIL_ACCOUNTS:
                continue
            for email in data.get('emails', []):
                email_subj = (email.get('subject', '') or '').lower()
                email_to = (email.get('to', '') or '').lower()
                email_from = (email.get('from', '') or '').lower()
                email_date = email.get('date', '')
                thread_id = email.get('thread_id', '')
                if not thread_id:
                    continue
                is_from_client = (recipient_lower in email_from or (recipient_domain and recipient_domain in email_from))
                is_to_client = (recipient_lower in email_to or (recipient_domain and recipient_domain in email_to))
                if not is_from_client and not is_to_client:
                    continue
                clean_email_subj = re.sub(r'^(re:\s*|fwd:\s*|\[.*?\]\s*)*', '', email_subj, flags=re.IGNORECASE).strip()
                subj_match = (clean_subj and clean_email_subj and (clean_subj in clean_email_subj or clean_email_subj in clean_subj))
                match_info = {
                    'account': account_email,
                    'thread_id': thread_id,
                    'subject': email.get('subject', ''),
                    'from': email_from,
                    'to': email_to,
                    'date': email_date,
                    'direction': 'incoming' if is_from_client else 'outgoing'
                }
                if subj_match and is_from_client:
                    matches.append((match_info, 3))
                elif subj_match and is_to_client:
                    matches.append((match_info, 2))
                elif is_from_client and not subj_match:
                    matches.append((match_info, 1))
        except:
            pass
    matches.sort(key=lambda x: (x[1], x[0]['date']), reverse=True)
    return matches

def search_api_thread(service, subject, recipient_email):
    clean_subj = re.sub(r'^(Re:\s*|Fwd:\s*|\[.*?\]\s*)*', '', subject, flags=re.IGNORECASE).strip()
    # 1. Search from client
    query1 = f'from:{recipient_email} subject:("{clean_subj}")'
    tid = gmail_api_client.search_thread_id(service, query1)
    if tid:
        return tid
        
    # 2. Broaden domain
    domain = recipient_email.split('@')[-1] if '@' in recipient_email else ''
    query2 = f'from:{domain} subject:("{clean_subj}")'
    tid2 = gmail_api_client.search_thread_id(service, query2)
    if tid2:
        return tid2
        
    # 3. Last resort sent
    query3 = f'in:sent to:{recipient_email} subject:("{clean_subj}")'
    tid3 = gmail_api_client.search_thread_id(service, query3)
    if tid3:
        return tid3
        
    # 4. Safe fallback
    query4 = f'to:{recipient_email} OR from:{recipient_email}'
    tid4 = gmail_api_client.search_thread_id(service, query4)
    if tid4:
        return tid4
        
    return None

def send_api_reply(thread_id, to_email, reply_html, subject, sender_email=None):
    if not sender_email:
        sender_email = DEFAULT_SENDER
        
    try:
        service = gmail_api_client.get_gmail_service(sender_email)
    except Exception as e:
        print(f"    ❌ Error initializing API client for {sender_email}: {e}", flush=True)
        return 'error'
        
    try:
        if not thread_id:
            print(f"    🔍 Searching for thread ID via API...", flush=True)
            thread_id = search_api_thread(service, subject, to_email)
            
        if not thread_id:
            print(f"    ❌ No thread found via API search", flush=True)
            return 'thread_not_found'
            
        print(f"    📧 Replying to thread {thread_id}...", flush=True)
        # 2026-09-15: a transient Gmail timeout on the account that OWNS the thread used to
        # fall through to the other personas (all 404 on a foreign thread id) and park the
        # ticket in "Unable to Send Email". Retry the same account once before giving up.
        last_err = None
        for attempt in (1, 2):
            try:
                gmail_api_client.send_reply(service, thread_id, to_email, subject, reply_html, sender_email)
                print(f"    ✅ Email sent via API!", flush=True)
                return 'sent'
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if attempt == 1 and ("timed out" in msg or "timeout" in msg or "connection" in msg):
                    print(f"    ⏳ Gmail timeout via {sender_email}, retrying once...", flush=True)
                    time.sleep(3)
                    continue
                raise
        raise last_err
    except Exception as e:
        print(f"    ❌ Error sending via API: {str(e)[:200]}", flush=True)
        return 'error'

def main():
    start_time = datetime.now()
    mode_str = '🤖 AUTO' if AUTO_MODE else ('🔴 DRY RUN' if DRY_RUN else '🟢 SENDING')
    
    logger.info('=' * 60)
    logger.info(f'📤 CRM Reply Email Sender (GMAIL API) — {start_time.strftime("%Y-%m-%d %H:%M:%S")}')
    logger.info(f'   Accounts: {", ".join(GMAIL_ACCOUNTS.keys())}')
    logger.info(f'   Mode: {mode_str}')
    logger.info('=' * 60)
    
    logger.info('🔗 Connecting to CRM...')
    
    load_odoo_signatures()
    load_user_mapping()
    logger.info(f'📝 Loaded {len(SIGNATURES)} signatures, {len(USER_TO_GMAIL)} user mappings from Odoo')
    session = crm_session()
    
    logger.info('⏰ Checking stale Follow Up x1 tickets...')
    stale_moved = check_followup_x1_stale(session)
    if stale_moved:
        logger.info(f'   📋 Moved {stale_moved} stale tickets to Reply Client')
    
    tickets = fetch_tickets_to_send(session)
    logger.info(f'📋 Found {len(tickets)} tickets to process')
    
    if not tickets:
        logger.info('✅ No tickets to send!')
        return
    
    for i, t in enumerate(tickets, 1):
        thread_id = extract_thread_id(t['name'])
        to_email = extract_recipient_email(t.get('email_from', ''))
        reply_preview = re.sub(r'<[^>]+>', '', t.get('reply_email') or '')[:100]
        
        logger.info(f'  {i}. [#{t["id"]}] {t["name"][:60]}')
        logger.info(f'     To: {to_email}')
        logger.info(f'     Thread: {thread_id}')
        logger.info(f'     Reply: {reply_preview}...')
    
    if DRY_RUN:
        logger.info('=' * 60)
        logger.info('🔴 DRY RUN — No emails sent. Run with --send or --auto to send.')
        logger.info('=' * 60)
        return
    
    logger.info('📧 Sending via Gmail API...')
    
    sent_count = 0
    failed_count = 0
    
    for i, ticket in enumerate(tickets, 1):
        email_field, field_html, dest_stage, stage_label = detect_email_field(ticket)
        
        if not email_field or not field_html:
            logger.info(f'   ⚠️ No email content in any field — skipping')
            continue
        
        thread_id = extract_thread_id(ticket['name'])
        to_email = extract_recipient_email(ticket.get('email_from', ''))
        reply_html = clean_reply_html(field_html)
        subject = ticket.get('name', 'No Subject')
        clean_subject = re.sub(r'\s*\[\s*thread::[^\]]*\]', '', subject).strip()
        
        if not to_email:
            logger.info(f'   ⚠️ No recipient email — skipping')
            failed_count += 1
            continue

        accounts_to_try = []
        
        logger.info(f'   🔍 Searching synced email JSONs for thread...')
        json_matches = find_threads_in_json(clean_subject, to_email)
        if json_matches:
            best_match = json_matches[0][0]
            if not thread_id:
                thread_id = best_match['thread_id']
            for m, score in json_matches:
                if m['account'] not in accounts_to_try:
                    accounts_to_try.append(m['account'])
        
        fb1 = detect_sender_from_user(ticket.get('user_id'))
        fb2 = detect_sender_from_reply(reply_html)
        if not fb2:
            fb2 = detect_sender_from_name_mention(reply_html)
        if not fb2:
            inbound_body = fetch_first_inbound_body(session, ticket['id'])
            fb2 = detect_sender_from_name_mention(inbound_body)

        persona_detected = bool(json_matches) or bool(fb1) or bool(fb2)

        for acc in [fb1, fb2, DEFAULT_SENDER]:
            if acc and acc not in accounts_to_try:
                accounts_to_try.append(acc)

        for acc in GMAIL_ACCOUNTS.keys():
            if acc not in accounts_to_try:
                accounts_to_try.append(acc)

        if not persona_detected:
            logger.warning(f'   ⚠️ No persona signal detected for #{ticket["id"]} — '
                            f'defaulting to {DEFAULT_SENDER}, flagging on the lead for manual review')
            post_sender_warning(session, ticket['id'], DEFAULT_SENDER)

        status = 'error'
        
        logger.info(f'\n{"="*50}')
        logger.info(f'📧 [{i}/{len(tickets)}] #{ticket["id"]}: {clean_subject[:50]}')
        logger.info(f'   Field: {email_field} → {stage_label}')
        logger.info(f'   To: {to_email}')
        logger.info(f'   Accounts to try: {", ".join(accounts_to_try[:3])}...')
        
        for try_email in accounts_to_try:
            temp_reply_html = append_signature(reply_html, try_email)
            
            logger.debug(f'Trying to send via {try_email}...')
            status = send_api_reply(thread_id, to_email, temp_reply_html, clean_subject, try_email)
            
            if status == 'sent':
                if len(temp_reply_html) > len(reply_html):
                    try:
                        session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/write", json={
                            "jsonrpc": "2.0", "method": "call",
                            "params": {
                                "model": "crm.lead", "method": "write",
                                "args": [[ticket['id']], {email_field: temp_reply_html}],
                                "kwargs": {}
                            }
                        })
                        logger.info(f'   ✍️ Signature inserted into Odoo {email_field}')
                    except:
                        pass
                break
            elif status == 'thread_not_found':
                logger.info(f"   ⚠️ Thread not found in {try_email}, trying next...")
            else:
                logger.info(f"   ⚠️ Error sending via {try_email}, trying next...")
                
        if status == 'sent':
            sent_count += 1
            sent_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            logger.info(f'   📋 Moving to {stage_label}...')
            if move_to_stage(session, ticket['id'], dest_stage):
                logger.info(f'   ✅ Moved to {stage_label}!')
            else:
                logger.warning(f'   ⚠️ Failed to move stage for #{ticket["id"]}')
            
            logger.info(f'   💬 Posting comment on ticket...')
            if post_ticket_comment(session, ticket['id'], to_email, reply_html, sent_time):
                logger.info(f'   ✅ Comment posted!')
            else:
                logger.warning(f'   ⚠️ Failed to post comment for #{ticket["id"]}')
            
            clear_email_field(session, ticket['id'], email_field)
            logger.info(f'   🧹 Cleared {email_field} field')
        else:
            failed_count += 1
            fail_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f'   ❌ Failed to send #{ticket["id"]} to {to_email}')
            
            logger.info(f'   ↩️ Moving to Unable to Send Email...')
            error_stage = get_error_stage(session)
            if move_to_stage(session, ticket['id'], error_stage):
                logger.info(f'   ✅ Moved to Unable to Send Email')
            else:
                logger.warning(f'   ⚠️ Failed to move back for #{ticket["id"]}')
            
            try:
                session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/message_post", json={
                    "jsonrpc": "2.0", "method": "call",
                    "params": {
                        "model": "crm.lead", "method": "message_post",
                        "args": [[ticket['id']]],
                        "kwargs": {
                            "body": f"⚠️ Auto-send failed at {fail_time}\n"
                                    f"To: {to_email}\n"
                                    f"Field: {email_field}\n"
                                    f"Error: No email thread found in Gmail\n"
                                    f"Action: Moved to Unable to Send Email for review",
                            "message_type": "comment",
                            "subtype_xmlid": "mail.mt_note",
                        }
                    }
                })
                logger.info(f'   💬 Error comment posted')
            except:
                pass
        
        time.sleep(2)
        
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f'\n{"="*60}')
    logger.info(f'📊 Results: {sent_count} sent, {failed_count} failed ({elapsed:.0f}s)')
    logger.info(f'{"="*60}')

if __name__ == '__main__':
    main()
