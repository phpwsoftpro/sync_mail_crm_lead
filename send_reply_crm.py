#!/usr/bin/env python3
"""send_reply_crm.py — Auto-send reply emails from CRM and move to Follow-up.

Flow:
1. Fetch all CRM leads in 'Send Email to Client' stage with reply_email filled
2. For each lead: 
   - Extract thread ID from lead name
   - Open Gmail, find the thread, reply with the reply_email body
   - Move the CRM lead to 'Enrich/Follow-up/ Other' stage
3. Uses Vanessa's Gmail to send (vanessa@wsoftpro.com)

Usage:
  python3 send_reply_crm.py            # Dry run — show what would be sent
  python3 send_reply_crm.py --send     # Actually send emails + move tickets
"""
import json
import time
import os
import sys
import re
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_DIR = os.path.join(SCRIPT_DIR, 'cookies')

# Load .env
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

# CRM Config
CRM_URL = ENV.get("CRM_URL", "https://crm.wsoftpro.com")
CRM_USER = ENV.get("CRM_USER", "")
CRM_PASS = ENV.get("CRM_PASSWORD", "")
CRM_DB = ENV.get("CRM_DB", "")

# Gmail sender
SENDER_EMAIL = ENV.get("GMAIL_VANESSA_EMAIL", "vanessa@wsoftpro.com")
SENDER_STORAGE = os.path.join(COOKIES_DIR, "gmail_vanessa_storage.json")

# CRM stages
STAGE_SEND_EMAIL = 7      # 'Send Email to Client'
STAGE_FOLLOWUP = 9        # 'Enrich/Follow-up/ Other'

STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
DRY_RUN = "--send" not in sys.argv


# ============================================================
# CRM Functions
# ============================================================
def crm_session():
    """Authenticate and return a requests session."""
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
    """Fetch leads in 'Send Email to Client' with reply_email filled."""
    res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/search_read", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {
            "model": "crm.lead", "method": "search_read",
            "args": [[[
                "reply_email", "!=", False
            ], [
                "active", "=", True
            ], [
                "stage_id", "=", STAGE_SEND_EMAIL
            ]]],
            "kwargs": {
                "fields": ["name", "email_from", "reply_email", "message_id", "partner_name"],
                "order": "id asc"
            }
        }
    })
    return res.json().get("result", [])


def move_to_followup(session, lead_id):
    """Move a CRM lead to the Follow-up stage."""
    res = session.post(f"{CRM_URL}/web/dataset/call_kw/crm.lead/write", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {
            "model": "crm.lead", "method": "write",
            "args": [[lead_id], {"stage_id": STAGE_FOLLOWUP}],
            "kwargs": {}
        }
    })
    return "error" not in res.json()


def extract_thread_id(name):
    """Extract Gmail thread ID from lead name like 'RE: ... [ thread::-EMwD0n3OhqQ... ]'"""
    match = re.search(r'thread::([^\s\]]+)', name or '')
    if match:
        tid = match.group(1).rstrip(':').strip()
        return tid
    return None


def extract_recipient_email(email_from):
    """Extract email address from 'Name <email>' format."""
    match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', email_from or '')
    return match.group(0) if match else ''


def clean_reply_html(html_body):
    """Clean Odoo HTML for Gmail compose."""
    if not html_body:
        return ''
    # Remove Odoo-specific attributes but keep the HTML structure
    html_body = re.sub(r'\s*data-oe-[a-z-]+="[^"]*"', '', html_body)
    return html_body


# ============================================================
# Gmail Thread Search — find original thread ID by subject/recipient
# ============================================================
def search_gmail_thread(page, subject, recipient_email):
    """Search Gmail for the original thread to reply on using Gmail's internal API."""
    import urllib.parse
    
    # Clean subject — remove Re:, Fwd:, [tags]
    clean_subj = re.sub(r'^(Re:\s*|Fwd:\s*|\[.*?\]\s*)*', '', subject, flags=re.IGNORECASE).strip()
    
    # Get Gmail ik token
    ik = page.evaluate('() => { try { return GLOBALS[9]; } catch(e) { return ""; } }')
    if not ik:
        print(f"    🔍 No Gmail ik token", flush=True)
        return None
    
    # Use Gmail internal API to search sent emails
    search_q = f'in:sent to:{recipient_email} subject:("{clean_subj}")'
    print(f"    🔍 Searching: \"{clean_subj[:35]}\" to {recipient_email}", flush=True)
    
    # Fetch thread list via Gmail API — returns thread IDs
    thread_id = page.evaluate('''async (args) => {
        const [ik, query] = args;
        try {
            const url = '/mail/u/0/?ik=' + ik + '&view=tl&start=0&num=5&rt=c&q=' + 
                        encodeURIComponent(query) + '&search=query';
            const resp = await fetch(url, {credentials: 'include'});
            const text = await resp.text();
            
            // Gmail response contains thread data in arrays
            // Thread IDs appear as hex strings (16 chars) in the response
            // Format: ["t","threadHexId", ...]
            const hexMatches = text.match(/"([0-9a-f]{16})"/g);
            if (hexMatches && hexMatches.length > 0) {
                // Return first thread hex ID (strip quotes)
                return hexMatches[0].replace(/"/g, '');
            }
            
            // Also try to find base64-like thread IDs
            const b64Matches = text.match(/"(FM[A-Za-z0-9_-]{20,})"/g);
            if (b64Matches && b64Matches.length > 0) {
                return b64Matches[0].replace(/"/g, '');
            }
            
            return null;
        } catch(e) {
            return null;
        }
    }''', [ik, search_q])
    
    if thread_id:
        print(f"    🔍 Thread found (hex): {thread_id}", flush=True)
        
        # Convert hex thread ID to Gmail URL thread ID by navigating
        page.goto(f'https://mail.google.com/mail/u/0/#inbox/{thread_id}', 
                  wait_until='domcontentloaded', timeout=30000)
        time.sleep(5)
        
        url = page.url
        m = re.search(r'[#/]([A-Za-z0-9_-]{15,})$', url)
        if m:
            url_tid = m.group(1)
            if url_tid != thread_id:
                print(f"    🔍 URL thread: {url_tid}", flush=True)
                return url_tid
        
        return thread_id
    
    # Broadened search
    search_q2 = f'in:sent subject:("{clean_subj}")'
    print(f"    🔍 Broadening search...", flush=True)
    
    thread_id2 = page.evaluate('''async (args) => {
        const [ik, query] = args;
        try {
            const url = '/mail/u/0/?ik=' + ik + '&view=tl&start=0&num=5&rt=c&q=' + 
                        encodeURIComponent(query) + '&search=query';
            const resp = await fetch(url, {credentials: 'include'});
            const text = await resp.text();
            
            const hexMatches = text.match(/"([0-9a-f]{16})"/g);
            if (hexMatches && hexMatches.length > 0) {
                return hexMatches[0].replace(/"/g, '');
            }
            return null;
        } catch(e) { return null; }
    }''', [ik, search_q2])
    
    if thread_id2:
        print(f"    🔍 Thread found (broad): {thread_id2}", flush=True)
        page.goto(f'https://mail.google.com/mail/u/0/#inbox/{thread_id2}',
                  wait_until='domcontentloaded', timeout=30000)
        time.sleep(5)
        url = page.url
        m = re.search(r'[#/]([A-Za-z0-9_-]{15,})$', url)
        if m:
            return m.group(1)
        return thread_id2
    
    print(f"    🔍 No thread found", flush=True)
    return None


# ============================================================
# Gmail Reply Function
# ============================================================
def send_gmail_reply(pw_instance, browser, thread_id, to_email, reply_html, subject):
    """Send reply via Gmail compose URL (same thread)."""
    storage_file = SENDER_STORAGE
    
    if not os.path.exists(storage_file):
        print(f"    ❌ No cookies for {SENDER_EMAIL}")
        return False
    
    ctx = browser.new_context(
        storage_state=storage_file,
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        viewport={'width': 1280, 'height': 900},
    )
    page = ctx.new_page()
    page.add_init_script(STEALTH_JS)
    # Bypass Gmail's Trusted Types policy so we can insert HTML
    page.add_init_script("""
        if (window.trustedTypes && window.trustedTypes.createPolicy) {
            try {
                window.trustedTypes.createPolicy('default', {
                    createHTML: (s) => s,
                    createScript: (s) => s,
                    createScriptURL: (s) => s,
                });
            } catch(e) {}
        }
    """)
    
    try:
        # Step 1: Load inbox to initialize Gmail JS context
        print(f"    📧 Loading Gmail...", flush=True)
        page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)
        
        title = page.title()
        if "inbox" not in title.lower() and "mail" not in title.lower() and "hộp thư" not in title.lower():
            print(f"    ❌ Gmail not loaded (title: {title})", flush=True)
            return False
        
        # Step 1.5: If no thread_id, search Gmail for the original thread
        if not thread_id:
            print(f"    🔍 No thread ID — searching Gmail for original thread...", flush=True)
            thread_id = search_gmail_thread(page, subject, to_email)
            if thread_id:
                print(f"    ✅ Found thread: {thread_id}", flush=True)
            else:
                print(f"    ⚠️  No thread found — will send as new email", flush=True)
        
        # Step 2: Open compose URL with thread reference for same-thread reply
        # Encode subject for URL
        import urllib.parse
        encoded_subject = urllib.parse.quote(subject)
        encoded_to = urllib.parse.quote(to_email)
        
        compose_url = (
            f"https://mail.google.com/mail/u/0/?tf=cm&fs=1"
            f"&to={encoded_to}"
            f"&su={encoded_subject}"
        )
        if thread_id:
            compose_url += f"&th={thread_id}"
            print(f"    📝 Opening compose (reply to thread)...", flush=True)
        else:
            print(f"    📝 Opening compose (new email, same subject)...", flush=True)
        page.goto(compose_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)
        
        # Step 3: Find the compose Message Body area
        compose = page.query_selector('div[aria-label="Message Body"]')
        if not compose:
            compose = page.query_selector('div[contenteditable="true"].Am')
        if not compose:
            compose = page.query_selector('div.editable[contenteditable="true"]')
        
        if not compose:
            print(f"    ❌ Could not find compose area", flush=True)
            return False
        
        print(f"    ✍️  Inserting reply body...", flush=True)
        
        # Step 4: Insert the reply HTML (bypass Trusted Types)
        result = page.evaluate('''(html) => {
            const compose = document.querySelector('div[aria-label="Message Body"]') ||
                           document.querySelector('div[contenteditable="true"].Am') ||
                           document.querySelector('div.editable[contenteditable="true"]');
            if (!compose) return false;
            
            compose.focus();
            
            // Select all existing content
            const sel = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(compose);
            sel.removeAllRanges();
            sel.addRange(range);
            
            // Use execCommand to insert HTML (bypasses Trusted Types)
            const ok = document.execCommand('insertHTML', false, html);
            if (ok) return true;
            
            // Fallback: use Trusted Types policy
            try {
                if (window.trustedTypes && window.trustedTypes.createPolicy) {
                    const policy = trustedTypes.createPolicy('myPolicy', { createHTML: (s) => s });
                    compose.innerHTML = policy.createHTML(html);
                    return true;
                }
            } catch(e) {}
            
            // Fallback 2: use DOMParser + appendChild
            try {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                compose.textContent = '';
                for (const child of [...doc.body.childNodes]) {
                    compose.appendChild(child.cloneNode(true));
                }
                return true;
            } catch(e) {}
            
            return false;
        }''', reply_html)
        
        if not result:
            print(f"    ❌ Failed to insert HTML", flush=True)
            return False
        
        print(f"    ✅ Reply body inserted", flush=True)
        time.sleep(2)
        
        # Step 5: Verify To field
        to_info = page.evaluate('''() => {
            // Check input fields for to
            const toInputs = document.querySelectorAll('input[aria-label="To recipients"]');
            for (const inp of toInputs) {
                if (inp.value) return inp.value;
            }
            // Check chips
            const chips = document.querySelectorAll('div.fX span[email]');
            const emails = [];
            for (const c of chips) { emails.push(c.getAttribute('email')); }
            if (emails.length) return emails.join(', ');
            // Check any to field
            const toDiv = document.querySelector('input[name="to"]');
            return toDiv ? toDiv.value : 'unknown';
        }''')
        print(f"    📨 To: {to_info}", flush=True)
        
        # Step 6: Click Send button
        send_btn = page.query_selector('div[aria-label="Send"][role="button"]')
        if not send_btn:
            send_btn = page.query_selector('div[data-tooltip="Send"]')
        if not send_btn:
            # Search by text
            all_btns = page.query_selector_all('div[role="button"]')
            for btn in all_btns:
                txt = (btn.inner_text() or '').strip().lower()
                if txt in ['send', 'gửi']:
                    send_btn = btn
                    break
        
        if send_btn:
            send_btn.click()
            print(f"    📤 Send clicked!", flush=True)
            time.sleep(5)
            
            # Check for "sent" confirmation
            body_text = page.evaluate("() => document.body?.innerText || ''")
            if 'sent' in body_text.lower() or 'đã gửi' in body_text.lower():
                print(f"    ✅ Email sent successfully!", flush=True)
            else:
                print(f"    ✅ Send clicked (checking...)", flush=True)
            
            # Save updated cookies
            ctx.storage_state(path=storage_file)
            return True
        else:
            # Fallback: Ctrl+Enter
            print(f"    ⌨️  Using Ctrl+Enter to send...", flush=True)
            compose.click()
            time.sleep(0.5)
            page.keyboard.press('Meta+Enter')
            time.sleep(5)
            ctx.storage_state(path=storage_file)
            return True
            
    except Exception as e:
        print(f"    ❌ Error: {str(e)[:150]}", flush=True)
        return False
    finally:
        page.close()
        ctx.close()


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("📤 CRM Reply Email Sender")
    print(f"   Sender: {SENDER_EMAIL}")
    print(f"   Mode: {'🔴 DRY RUN' if DRY_RUN else '🟢 SENDING'}")
    print("=" * 60)
    print()
    
    # 1. Get tickets
    print("🔗 Connecting to CRM...", flush=True)
    session = crm_session()
    tickets = fetch_tickets_to_send(session)
    print(f"📋 Found {len(tickets)} tickets to process", flush=True)
    print()
    
    if not tickets:
        print("✅ No tickets to send!")
        return
    
    # Show tickets
    for i, t in enumerate(tickets, 1):
        thread_id = extract_thread_id(t['name'])
        to_email = extract_recipient_email(t.get('email_from', ''))
        reply_preview = re.sub(r'<[^>]+>', '', t.get('reply_email', ''))[:100]
        
        print(f"  {i}. [#{t['id']}] {t['name'][:60]}")
        print(f"     To: {to_email}")
        print(f"     Thread: {thread_id}")
        print(f"     Reply: {reply_preview}...")
        print()
    
    if DRY_RUN:
        print("=" * 60)
        print("🔴 DRY RUN — No emails sent. Run with --send to actually send.")
        print("   python3 send_reply_crm.py --send")
        print("=" * 60)
        return
    
    # 2. Send emails via Gmail
    print("📧 Starting Gmail...", flush=True)
    
    sent_count = 0
    failed_count = 0
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        
        for i, ticket in enumerate(tickets, 1):
            thread_id = extract_thread_id(ticket['name'])
            to_email = extract_recipient_email(ticket.get('email_from', ''))
            reply_html = clean_reply_html(ticket.get('reply_email', ''))
            subject = ticket.get('name', 'No Subject')
            
            # Clean subject (remove thread:: part)
            clean_subject = re.sub(r'\s*\[\s*thread::[^\]]*\]', '', subject).strip()
            
            print(f"\n{'='*50}")
            print(f"📧 [{i}/{len(tickets)}] #{ticket['id']}: {clean_subject[:50]}")
            print(f"   To: {to_email}")
            
            if not thread_id:
                print(f"   ℹ️  No thread ID — will send as new email with matching subject")
            
            if not to_email:
                print(f"   ⚠️ No recipient email — skipping")
                failed_count += 1
                continue
            
            # Send reply
            success = send_gmail_reply(pw, browser, thread_id, to_email, reply_html, clean_subject)
            
            if success:
                sent_count += 1
                # Move to follow-up
                print(f"   📋 Moving to Follow-up stage...", flush=True)
                if move_to_followup(session, ticket['id']):
                    print(f"   ✅ Moved to Follow-up!", flush=True)
                else:
                    print(f"   ⚠️ Failed to move stage", flush=True)
            else:
                failed_count += 1
                print(f"   ❌ Failed to send", flush=True)
            
            # Small delay between sends
            time.sleep(2)
        
        browser.close()
    
    print(f"\n{'='*60}")
    print(f"📊 Results: {sent_count} sent, {failed_count} failed")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
