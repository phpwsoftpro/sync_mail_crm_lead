#!/usr/bin/env python3
"""Compare Gmail emails with CRM pipeline tickets.
- Fetch fresh CRM leads from Odoo
- Load Gmail JSON files (Robert, Vanessa, Luna, Helen)
- Match each email to a CRM lead (by sender email/name/subject)
- Report missing tickets
- Optionally create new CRM leads for unmatched emails
"""
import requests
import json
import re
import os
from datetime import datetime

# ============================================================
# CONFIG (loaded from .env)
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_env(env_path=None):
    if env_path is None:
        env_path = os.path.join(SCRIPT_DIR, '.env')
    _env = {}
    if not os.path.exists(env_path):
        print(f"⚠️  No .env file found at {env_path}")
        import sys; sys.exit(1)
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

EMAILS_DIR = os.path.join(SCRIPT_DIR, 'emails')
OUTPUT_DIR = SCRIPT_DIR

GMAIL_FILES = [
    "gmail_robert_emails.json",
    "gmail_vanessa_emails.json",
    "gmail_luna_emails.json",
    "gmail_helen_emails.json",
]

# Emails to ignore (internal, system, no-reply)
IGNORE_SENDERS = [
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "accounts.google.com", "google.com", "calendar-notification",
    "notifications", "support@google", "drive-shares-dm-noreply",
    "wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com",
    "calendar-server", "group-digests-noreply",
]


def extract_email_address(from_str):
    """Extract email address from 'Name <email@domain.com>' format."""
    match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', from_str or '')
    return match.group(0).lower() if match else ''


def extract_sender_name(from_str):
    """Extract name from 'Name <email@domain.com>' format."""
    match = re.match(r'^([^<]+)<', from_str or '')
    if match:
        return match.group(1).strip().strip('"\'')
    return from_str.strip() if from_str else ''


def is_internal_email(from_str):
    """Check if email is from internal/system senders."""
    from_lower = (from_str or '').lower()
    return any(s in from_lower for s in IGNORE_SENDERS)


# ============================================================
# 1. FETCH CRM LEADS
# ============================================================
def fetch_crm_leads():
    """Fetch all active CRM leads from Odoo."""
    session = requests.Session()
    
    # Authenticate
    auth_res = session.post(f"{CRM_URL}/web/session/authenticate", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {"db": CRM_DB, "login": CRM_USER, "password": CRM_PASS}
    })
    auth_data = auth_res.json()
    if "error" in auth_data:
        print(f"❌ CRM auth failed: {auth_data['error']}")
        return []
    
    # Fetch ALL leads (active + inactive for full coverage)
    leads = []
    for active in [True, False]:
        res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/search_read", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": "crm.lead", "method": "search_read",
                "args": [[[  "active", "=", active]]],
                "kwargs": {
                    "fields": ["name", "stage_id", "partner_name", "email_from", 
                              "contact_name", "description", "create_date", "user_id",
                              "expected_revenue", "probability"],
                    "limit": 5000
                }
            }
        })
        leads.extend(res.json().get("result", []))
    
    print(f"📊 Fetched {len(leads)} CRM leads (active + archived)")
    return leads, session


def create_crm_lead(session, email_data, gmail_account):
    """Create a new CRM lead from an unmatched email."""
    sender_email = extract_email_address(email_data.get('from', ''))
    sender_name = extract_sender_name(email_data.get('from', ''))
    subject = email_data.get('subject', 'No Subject')
    body_preview = email_data.get('body_text', '')[:500]
    
    lead_payload = {
        "jsonrpc": "2.0", "method": "call",
        "params": {
            "model": "crm.lead", "method": "create",
            "args": [{
                "name": subject or f"Email from {sender_name}",
                "partner_name": sender_name or sender_email,
                "email_from": sender_email,
                "contact_name": sender_name,
                "description": f"Auto-created from email sync\n"
                              f"Gmail: {gmail_account}\n"
                              f"From: {email_data.get('from', '')}\n"
                              f"Date: {email_data.get('date', '')}\n"
                              f"---\n{body_preview}",
                "type": "lead",
            }],
            "kwargs": {}
        }
    }
    
    res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/create", json=lead_payload)
    result = res.json()
    if "error" in result:
        return None, result["error"]
    return result.get("result"), None


# ============================================================
# 2. LOAD GMAIL EMAILS
# ============================================================
def load_gmail_emails():
    """Load all Gmail JSON files."""
    all_emails = []
    for fname in GMAIL_FILES:
        fpath = os.path.join(EMAILS_DIR, fname)
        if not os.path.exists(fpath):
            print(f"  ⚠️ Missing: {fname}")
            continue
        with open(fpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        account = data.get('account', '')
        for email in data.get('emails', []):
            email['_gmail_account'] = account
            email['_file'] = fname
        all_emails.extend(data.get('emails', []))
        print(f"  📧 {fname}: {data.get('count', 0)} emails")
    return all_emails


# ============================================================
# 3. MATCH EMAILS TO CRM LEADS
# ============================================================
def match_emails_to_leads(emails, leads):
    """Match each email to a CRM lead. Returns matched and unmatched lists."""
    
    # Build CRM lookup indexes
    lead_emails = {}  # email_from → lead
    lead_names = {}   # partner_name → lead
    lead_subjects = set()  # all lead names (subjects)
    
    for lead in leads:
        email_from = (lead.get('email_from') or '').lower().strip()
        partner = (lead.get('partner_name') or '').lower().strip()
        lead_name = (lead.get('name') or '').lower().strip()
        
        if email_from:
            lead_emails[email_from] = lead
        if partner:
            lead_names[partner] = lead
        if lead_name:
            lead_subjects.add(lead_name)
    
    matched = []
    unmatched = []
    
    for email in emails:
        if is_internal_email(email.get('from', '')):
            continue  # Skip internal/system emails
        
        sender_email = extract_email_address(email.get('from', ''))
        sender_name = extract_sender_name(email.get('from', '')).lower()
        subject = (email.get('subject', '') or '').lower().strip()
        
        # Try matching
        match = None
        match_type = None
        
        # 1. Match by sender email
        if sender_email in lead_emails:
            match = lead_emails[sender_email]
            match_type = "email"
        
        # 2. Match by sender name
        elif sender_name and sender_name in lead_names:
            match = lead_names[sender_name]
            match_type = "name"
        
        # 3. Match by subject
        elif subject and subject in lead_subjects:
            match = True
            match_type = "subject"
        
        # 4. Fuzzy: check if any part of email sender appears in CRM
        elif sender_email:
            domain = sender_email.split('@')[-1] if '@' in sender_email else ''
            for crm_email, lead in lead_emails.items():
                if domain and domain != 'gmail.com' and domain != 'outlook.com':
                    if domain in crm_email:
                        match = lead
                        match_type = "domain"
                        break
        
        if match:
            matched.append({
                'email': email,
                'crm_lead': match if isinstance(match, dict) else None,
                'match_type': match_type,
            })
        else:
            unmatched.append(email)
    
    return matched, unmatched


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("📊 Gmail ↔ CRM Pipeline Comparison")
    print("=" * 60)
    print()
    
    # 1. Fetch CRM leads
    print("🔗 Fetching CRM leads...")
    leads, session = fetch_crm_leads()
    print()
    
    # 2. Load Gmail emails
    print("📧 Loading Gmail emails...")
    emails = load_gmail_emails()
    print(f"  Total: {len(emails)} emails across {len(GMAIL_FILES)} accounts")
    print()
    
    # 3. Match
    print("🔍 Matching emails to CRM leads...")
    matched, unmatched = match_emails_to_leads(emails, leads)
    
    external_total = len(matched) + len(unmatched)
    print(f"  📊 External emails: {external_total}")
    print(f"  ✅ Matched: {len(matched)}")
    print(f"  ❌ Unmatched: {len(unmatched)}")
    print()
    
    # 4. Report
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "crm_leads_total": len(leads),
        "gmail_emails_total": len(emails),
        "external_emails": external_total,
        "matched": len(matched),
        "unmatched": len(unmatched),
        "matched_details": [],
        "unmatched_details": [],
    }
    
    # Matched details
    for m in matched:
        e = m['email']
        crm = m.get('crm_lead')
        report['matched_details'].append({
            'email_from': e.get('from', ''),
            'email_subject': e.get('subject', ''),
            'email_date': e.get('date', ''),
            'gmail_account': e.get('_gmail_account', ''),
            'match_type': m['match_type'],
            'crm_lead_name': crm.get('name', '') if crm else '',
            'crm_stage': crm['stage_id'][1] if crm and crm.get('stage_id') else '',
        })
    
    # Unmatched details
    for e in unmatched:
        report['unmatched_details'].append({
            'email_from': e.get('from', ''),
            'email_subject': e.get('subject', ''),
            'email_date': e.get('date', ''),
            'gmail_account': e.get('_gmail_account', ''),
            'body_preview': (e.get('body_text', '') or '')[:200],
        })
    
    # Save report
    report_path = os.path.join(OUTPUT_DIR, "crm_email_comparison.json")
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"📄 Report saved: {report_path}")
    print()
    
    # Print unmatched emails
    if unmatched:
        print("=" * 60)
        print(f"❌ UNMATCHED EMAILS ({len(unmatched)} need tickets)")
        print("=" * 60)
        for i, e in enumerate(unmatched, 1):
            print(f"  {i}. [{e.get('_gmail_account','')}]")
            print(f"     From: {e.get('from', '')}")
            print(f"     Subject: {e.get('subject', '')}")
            print(f"     Date: {e.get('date', '')}")
            print()
        
        print(f"\n💡 Run with --create flag to auto-create CRM leads for these emails")
    else:
        print("✅ All external emails have corresponding CRM tickets!")
    
    # Auto-create if flag set
    if "--create" in __import__("sys").argv:
        print(f"\n🔧 Creating {len(unmatched)} CRM leads...")
        created = 0
        for e in unmatched:
            lead_id, err = create_crm_lead(session, e, e.get('_gmail_account', ''))
            if lead_id:
                print(f"  ✅ Created lead #{lead_id}: {e.get('subject','')[:50]}")
                created += 1
            else:
                print(f"  ❌ Failed: {e.get('subject','')[:50]} - {err}")
        print(f"\n✅ Created {created}/{len(unmatched)} leads")


if __name__ == '__main__':
    main()
