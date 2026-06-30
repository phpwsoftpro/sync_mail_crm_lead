#!/usr/bin/env python3
"""syncmail — Fetch emails from all accounts.
- Loads inbox with saved cookies
- If cookies work → fetch emails directly (fast path)
- If cookies expired → relogin (visible browser for 2FA) → fetch
- Gmail: view=om API for full content
- Outlook: click-to-read with iframe body extraction
"""
import json
import time
import os
import sys
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_DIR = os.path.join(SCRIPT_DIR, 'cookies')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'emails')
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)

today = datetime.now()
yesterday = today - timedelta(days=1)

STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"

# Load .env file
def load_env(env_path=None):
    if env_path is None:
        env_path = os.path.join(SCRIPT_DIR, '.env')
    _env = {}
    if not os.path.exists(env_path):
        print(f"⚠️  No .env file found at {env_path}")
        print(f"   Copy .env.example to .env and fill in credentials")
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

def e(key, default=''):
    return ENV.get(key, default)

GMAIL_ACCOUNTS = [
    {"email": e("GMAIL_ROBERT_EMAIL"), "password": e("GMAIL_ROBERT_PASSWORD"), "name": e("GMAIL_ROBERT_NAME", "Robert"), "storage": "gmail_robert_storage.json"},
    {"email": e("GMAIL_VANESSA_EMAIL"), "password": e("GMAIL_VANESSA_PASSWORD"), "name": e("GMAIL_VANESSA_NAME", "Vanessa"), "storage": "gmail_vanessa_storage.json"},
    {"email": e("GMAIL_LUNA_EMAIL"), "password": e("GMAIL_LUNA_PASSWORD"), "name": e("GMAIL_LUNA_NAME", "Luna"), "storage": "gmail_luna_storage.json"},
    {"email": e("GMAIL_HELEN_EMAIL"), "password": e("GMAIL_HELEN_PASSWORD"), "name": e("GMAIL_HELEN_NAME", "Helen"), "storage": "gmail_helen_storage.json"},
]

OUTLOOK_ACCOUNTS = [
    {"email": e("OUTLOOK_TRAVIS_EMAIL"), "password": e("OUTLOOK_TRAVIS_PASSWORD"), "name": e("OUTLOOK_TRAVIS_NAME", "Travis"), "storage": "outlook_travis_storage.json"},
    {"email": e("OUTLOOK_TIMMY_EMAIL"), "password": e("OUTLOOK_TIMMY_PASSWORD"), "name": e("OUTLOOK_TIMMY_NAME", "Timmy"), "storage": "outlook_timmy_storage.json"},
    {"email": e("OUTLOOK_CLAYTON_EMAIL"), "password": e("OUTLOOK_CLAYTON_PASSWORD"), "name": e("OUTLOOK_CLAYTON_NAME", "Clayton"), "storage": "outlook_clayton_storage.json"},
    {"email": e("OUTLOOK_KIEU_EMAIL"), "password": e("OUTLOOK_KIEU_PASSWORD"), "name": e("OUTLOOK_KIEU_NAME", "Kieu"), "storage": "outlook_kieu_storage.json"},
    {"email": e("OUTLOOK_PETER_EMAIL"), "password": e("OUTLOOK_PETER_PASSWORD"), "name": e("OUTLOOK_PETER_NAME", "Peter"), "storage": "outlook_peter_storage.json"},
    {"email": e("OUTLOOK_RUSLAN_EMAIL"), "password": e("OUTLOOK_RUSLAN_PASSWORD"), "name": e("OUTLOOK_RUSLAN_NAME", "Ruslan"), "storage": "outlook_ruslan_storage.json"},
]

# JS for Gmail email extraction (view=om API)
GMAIL_FETCH_JS = '''async () => {
    let ik = '';
    try { ik = GLOBALS[9]; } catch(e) {}
    if (!ik) return { error: 'no ik', emails: [] };
    
    const rows = document.querySelectorAll('tr.zA');
    const threadIds = [];
    for (const row of rows) {
        const jslog = row.getAttribute('jslog') || '';
        const b64Match = jslog.match(/1:([A-Za-z0-9+\\/=]+)/);
        if (b64Match) {
            try {
                const decoded = atob(b64Match[1]);
                const tidMatch = decoded.match(/thread-[fa]:(\\w+)/);
                if (tidMatch) {
                    const hexId = BigInt(tidMatch[1]).toString(16);
                    threadIds.push({ hexId });
                }
            } catch(e) {}
        }
    }
    
    const results = [];
    const batchSize = 10;
    for (let b = 0; b < threadIds.length; b += batchSize) {
        const batch = threadIds.slice(b, b + batchSize);
        const promises = batch.map(async (t, idx) => {
            try {
                const url = '/mail/u/0/?ui=2&ik=' + ik + '&view=om&th=' + t.hexId;
                const resp = await fetch(url, {credentials: 'include'});
                const html = await resp.text();
                
                let from_ = '', to = '', date = '', subject = '', messageId = '';
                const thTdRegex = /<th>(.*?)<\\/th>\\s*<td[^>]*>(.*?)<\\/td>/gsi;
                let m;
                while ((m = thTdRegex.exec(html)) !== null) {
                    const key = m[1].replace(/<[^>]+>/g, '').trim().toLowerCase();
                    const val = m[2].replace(/<[^>]+>/g, '').trim();
                    if (key.includes('from') || key.includes('từ')) from_ = val;
                    else if (key.includes('to') || key.includes('đến')) to = val;
                    else if (key.includes('date') || key.includes('ngày') || key.includes('created')) date = val;
                    else if (key.includes('subject') || key.includes('chủ đề')) subject = val;
                    else if (key.includes('message id')) messageId = val;
                }
                
                let bodyHtml = '', bodyText = '';
                const tableEnd = html.indexOf('</table>');
                if (tableEnd > 0) {
                    bodyHtml = html.substring(tableEnd + 8)
                        .replace(/<\\/div>\\s*<\\/div>\\s*<\\/body>\\s*<\\/html>\\s*$/si, '').trim();
                }
                bodyText = (bodyHtml || '').replace(/<style[^>]*>.*?<\\/style>/gsi, '')
                    .replace(/<script[^>]*>.*?<\\/script>/gsi, '')
                    .replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&')
                    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/\\s+/g, ' ').trim();
                
                return { index: b + idx + 1, thread_id: t.hexId, from: from_, to, date, subject,
                         message_id: messageId,
                         body_text: bodyText.substring(0, 30000), body_html: bodyHtml.substring(0, 50000) };
            } catch(e) {
                return { index: b + idx + 1, thread_id: t.hexId, error: e.message };
            }
        });
        const batchResults = await Promise.all(promises);
        results.push(...batchResults);
    }
    return { ik, threadCount: threadIds.length, emails: results };
}'''

results = {}


# ============================================================
# RELOGIN FUNCTIONS (only called when cookies expired)
# ============================================================

def relogin_gmail(playwright_instance, acc):
    """Relogin to Gmail with visible browser for 2FA."""
    email, password, name = acc["email"], acc["password"], acc["name"]
    storage_file = os.path.join(COOKIES_DIR, acc["storage"])
    
    print(f"  🔑 Opening browser for re-login (2FA needed)...", flush=True)
    browser2 = playwright_instance.chromium.launch(headless=False)
    ctx = browser2.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 800},
    )
    page = ctx.new_page()
    page.add_init_script(STEALTH_JS)

    try:
        page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        print(f"  🔑 Entering email...", flush=True)
        email_input = page.wait_for_selector('#identifierId', state='visible', timeout=30000)
        email_input.click()
        time.sleep(0.5)
        page.keyboard.type(email, delay=50)
        time.sleep(1)
        page.click('#identifierNext button', timeout=10000)
        time.sleep(5)

        print(f"  🔑 Entering password...", flush=True)
        pwd_input = page.wait_for_selector('input[name="Passwd"]', state='visible', timeout=30000)
        pwd_input.click()
        time.sleep(0.5)
        page.keyboard.type(password, delay=50)
        time.sleep(1)
        page.click('#passwordNext button', timeout=10000)

        print(f"  ⏳ APPROVE 2FA ON PHONE! (waiting 180s)...", flush=True)
        try:
            page.wait_for_url(lambda url: "accounts.google.com" not in url, timeout=180000)
            print(f"  ✅ 2FA approved!", flush=True)
        except:
            print(f"  ⚠️ 2FA timeout, trying anyway...", flush=True)

        page.goto("https://mail.google.com/mail/u/0/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)
        title = page.title()

        if "inbox" in title.lower() or "hộp thư" in title.lower() or email.split("@")[0] in title.lower():
            ctx.storage_state(path=storage_file)
            print(f"  ✅ Re-login success! Cookies saved.", flush=True)
            page.close(); ctx.close(); browser2.close()
            return True
        else:
            print(f"  ❌ Re-login failed (title: {title})", flush=True)
            page.close(); ctx.close(); browser2.close()
            return False
    except Exception as e:
        print(f"  ❌ Re-login error: {str(e)[:150]}", flush=True)
        try: page.close()
        except: pass
        ctx.close(); browser2.close()
        return False


def relogin_outlook(playwright_instance, acc):
    """Relogin to Outlook with visible browser + passkey bypass."""
    email, password, name = acc["email"], acc["password"], acc["name"]
    storage_file = os.path.join(COOKIES_DIR, acc["storage"])

    print(f"  🔑 Opening browser for re-login...", flush=True)
    browser2 = playwright_instance.chromium.launch(headless=False)
    ctx = browser2.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 800},
    )
    page = ctx.new_page()
    page.add_init_script(STEALTH_JS)

    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send("WebAuthn.addVirtualAuthenticator", {
        "options": {"protocol": "ctap2", "transport": "internal",
                    "hasResidentKey": False, "hasUserVerification": False, "isUserVerified": False}
    })

    try:
        page.goto("https://login.live.com/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        print(f"  🔑 Entering email...", flush=True)
        email_input = page.wait_for_selector('#usernameEntry', state='visible', timeout=30000)
        email_input.click()
        time.sleep(0.5)
        page.keyboard.type(email, delay=50)
        page.keyboard.press("Enter")
        time.sleep(8)

        # Handle passkey prompt
        body_text = page.evaluate("() => document.body?.innerText || ''")
        if "passkey" in body_text.lower() or "Use your" in body_text:
            try:
                btn = page.query_selector('a#signInAnotherWay') or page.query_selector('[data-testid="otherSignInOptions"]')
                if btn: btn.click(); time.sleep(3)
                pwd_btn = page.query_selector('[data-testid="passwordButton"]') or page.query_selector('div[data-value="pwd"]')
                if pwd_btn: pwd_btn.click(); time.sleep(3)
            except:
                page.keyboard.press("Escape"); time.sleep(3)

        print(f"  🔑 Entering password...", flush=True)
        pwd_field = None
        for sel in ['#passwordEntry', 'input[name="passwd"]', '#i0118', 'input[type="password"]']:
            el = page.query_selector(sel)
            if el and el.is_visible(): pwd_field = el; break
        
        if pwd_field:
            pwd_field.click(); time.sleep(0.5)
            page.keyboard.type(password, delay=50)
            page.keyboard.press("Enter")
        else:
            print(f"  ❌ No password field", flush=True)
            page.close(); ctx.close(); browser2.close()
            return False

        print(f"  ⏳ Waiting for redirect (60s)...", flush=True)
        time.sleep(10)

        # "Stay signed in?"
        body_text = page.evaluate("() => document.body?.innerText || ''")
        if "Stay signed in" in body_text or "Duy trì" in body_text:
            try:
                yes_btn = page.query_selector('#acceptButton') or page.query_selector('input[value="Yes"]')
                if yes_btn: yes_btn.click(); time.sleep(5)
            except: pass

        page.goto("https://outlook.live.com/mail/0/inbox", wait_until="domcontentloaded", timeout=60000)
        time.sleep(10)
        title = page.title()

        if "mail" in title.lower() or "thư" in title.lower() or "outlook" in title.lower():
            ctx.storage_state(path=storage_file)
            print(f"  ✅ Re-login success! Cookies saved.", flush=True)
            page.close(); ctx.close(); browser2.close()
            return True
        else:
            print(f"  ❌ Re-login failed (title: {title})", flush=True)
            page.close(); ctx.close(); browser2.close()
            return False
    except Exception as e:
        print(f"  ❌ Re-login error: {str(e)[:150]}", flush=True)
        try: page.close()
        except: pass
        ctx.close(); browser2.close()
        return False


# ============================================================
# FETCH FUNCTIONS (load once, check inline, relogin if needed)
# ============================================================

def process_gmail(pw, browser, acc):
    """Load inbox once. If cookies work → fetch. If expired → relogin → fetch."""
    storage_path = os.path.join(COOKIES_DIR, acc["storage"])
    
    if not os.path.exists(storage_path):
        print(f"  ⚠️ No cookies found, need login first", flush=True)
        if not relogin_gmail(pw, acc):
            return None
    
    # Try loading with saved cookies
    ctx = browser.new_context(
        storage_state=storage_path,
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        viewport={'width': 1400, 'height': 900},
    )
    page = ctx.new_page()

    try:
        page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded", timeout=60000)
        time.sleep(15)
        title = page.title()
        has_ik = page.evaluate("() => { try { return !!GLOBALS[9]; } catch(e) { return false; } }")

        # Check if cookies worked
        if not has_ik or ("inbox" not in title.lower() and "hộp thư" not in title.lower() and acc["email"].split("@")[0] not in title.lower()):
            print(f"  ⚠️ Cookies expired (title: {title})", flush=True)
            page.close(); ctx.close()
            
            # Relogin with visible browser
            if not relogin_gmail(pw, acc):
                return None
            
            # Retry with new cookies
            ctx = browser.new_context(
                storage_state=storage_path,
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                viewport={'width': 1400, 'height': 900},
            )
            page = ctx.new_page()
            page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded", timeout=60000)
            time.sleep(15)
        
        print(f"  ✅ Cookies OK → fetching emails...", flush=True)
        
        # Fetch emails
        all_emails = page.evaluate(GMAIL_FETCH_JS)
        emails = all_emails.get('emails', [])
        total = all_emails.get('threadCount', 0)
        print(f"  📊 threads={total}, fetched={len(emails)}", flush=True)
        return emails

    except Exception as e:
        print(f"  ❌ {str(e)[:200]}", flush=True)
        return None
    finally:
        try: page.close()
        except: pass
        try: ctx.close()
        except: pass


def process_outlook(pw, browser, acc):
    """Load inbox once. If cookies work → fetch. If expired → relogin → fetch."""
    storage_path = os.path.join(COOKIES_DIR, acc["storage"])

    if not os.path.exists(storage_path):
        print(f"  ⚠️ No cookies found, need login first", flush=True)
        if not relogin_outlook(pw, acc):
            return None

    ctx = browser.new_context(
        storage_state=storage_path,
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        viewport={'width': 1400, 'height': 900},
    )
    page = ctx.new_page()

    try:
        page.goto("https://outlook.live.com/mail/0/inbox", wait_until="domcontentloaded", timeout=60000)
        time.sleep(12)
        title = page.title()

        # Check if cookies expired (redirected to login)
        if "microsoft-365" in page.url or "login" in page.url or "microsoft.com/en" in page.url:
            print(f"  ⚠️ Cookies expired (redirected to login)", flush=True)
            page.close(); ctx.close()
            
            if not relogin_outlook(pw, acc):
                return None
            
            ctx = browser.new_context(
                storage_state=storage_path,
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                viewport={'width': 1400, 'height': 900},
            )
            page = ctx.new_page()
            page.goto("https://outlook.live.com/mail/0/inbox", wait_until="domcontentloaded", timeout=60000)
            time.sleep(12)
            title = page.title()

        if "mail" not in title.lower() and "thư" not in title.lower() and "outlook" not in title.lower():
            print(f"  ❌ Not in inbox (title: {title})", flush=True)
            return None

        print(f"  ✅ Cookies OK → fetching emails...", flush=True)

        # Get email list
        email_items = page.evaluate("""() => {
            const items = document.querySelectorAll('[role="option"], [data-convid]');
            const skip = ['file', 'navigation', 'other', 'khác', 'tệp', 'focused'];
            const emails = [];
            for (const item of items) {
                const text = (item.innerText || '').trim();
                if (text.length < 20) continue;
                if (skip.some(s => text.toLowerCase().startsWith(s))) continue;
                const lines = text.split('\\n').filter(l => l.trim());
                emails.push({ from: lines[0] || '', subject: lines[1] || '',
                              preview: lines.slice(2).join(' ').substring(0, 300) });
                if (emails.length >= 20) break;
            }
            return emails;
        }""")
        print(f"  📊 Found {len(email_items)} emails, reading each...", flush=True)

        # Click each email for body
        full_emails = []
        for i in range(min(len(email_items), 20)):
            item = email_items[i]
            try:
                page.evaluate(f"""() => {{
                    const items = document.querySelectorAll('[role="option"], [data-convid]');
                    const skip = ['file', 'navigation', 'other', 'khác', 'tệp'];
                    let count = 0;
                    for (const el of items) {{
                        const text = (el.innerText || '').trim();
                        if (text.length < 20) continue;
                        if (skip.some(s => text.toLowerCase().startsWith(s))) continue;
                        if (count === {i}) {{ el.click(); return true; }}
                        count++;
                    }}
                }}""")
                time.sleep(2)
                
                detail = page.evaluate("""() => {
                    let subject = '', from_ = '', bodyText = '', bodyHtml = '';
                    const h = document.querySelector('[role="main"] [role="heading"], [role="main"] h1, [role="main"] h2');
                    if (h) { const t = (h.innerText||'').trim(); if (!t.includes('Inbox') && !t.includes('Hộp') && t.length > 2) subject = t; }
                    const main = document.querySelector('[role="main"]');
                    if (main) { for (const s of main.querySelectorAll('span')) { const t = (s.innerText||'').trim(); if (t.includes('@') && t.length < 80) { from_ = t; break; } } }
                    for (const iframe of document.querySelectorAll('iframe')) {
                        try { const doc = iframe.contentDocument || iframe.contentWindow?.document;
                              if (doc?.body) { const t = doc.body.innerText||''; if (t.length > bodyText.length) { bodyText = t; bodyHtml = doc.body.innerHTML; } }
                        } catch(e) {} }
                    if (!bodyText && main) { const doc = main.querySelector('[role="document"]'); if (doc) { bodyText = doc.innerText||''; bodyHtml = doc.innerHTML||''; } }
                    return { subject, from: from_, body_text: bodyText.substring(0, 30000), body_html: bodyHtml.substring(0, 50000) };
                }""")
                
                full_emails.append({
                    'index': i + 1,
                    'from': detail.get('from', '') or item.get('from', ''),
                    'subject': detail.get('subject', '') or item.get('subject', ''),
                    'body_preview': item.get('preview', ''),
                    'body_text': detail.get('body_text', ''),
                    'body_html': detail.get('body_html', ''),
                })
            except Exception as e:
                full_emails.append({'index': i+1, 'from': item.get('from',''), 'subject': item.get('subject',''), 'error': str(e)[:80]})

        return full_emails

    except Exception as e:
        print(f"  ❌ {str(e)[:200]}", flush=True)
        return None
    finally:
        try: page.close()
        except: pass
        try: ctx.close()
        except: pass


# ============================================================
# MAIN
# ============================================================
def main():
    print(f"📧 syncmail — {yesterday.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}")
    print(f"{'='*60}\n")
    
    start_time = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        for acc in GMAIL_ACCOUNTS:
            print(f"📧 GMAIL: {acc['email']} ({acc['name']})")
            t0 = time.time()
            try:
                emails = process_gmail(pw, browser, acc)
                elapsed = time.time() - t0
                if emails is not None:
                    out = os.path.join(OUTPUT_DIR, f"gmail_{acc['name'].lower()}_emails.json")
                    with open(out, 'w', encoding='utf-8') as f:
                        json.dump({'account': acc['email'], 'name': acc['name'], 'type': 'gmail',
                                   'date_range': f"{yesterday.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}",
                                   'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                                   'count': len(emails), 'emails': emails}, f, indent=2, ensure_ascii=False)
                    print(f"  ✅ {len(emails)} emails → {out} ({elapsed:.0f}s)", flush=True)
                    results[acc['email']] = f"✅ {len(emails)} emails"
                else:
                    results[acc['email']] = "❌ failed"
            except Exception as e:
                print(f"  ❌ {str(e)[:100]}", flush=True)
                results[acc['email']] = "❌ error"
            print()

        for acc in OUTLOOK_ACCOUNTS:
            print(f"📬 OUTLOOK: {acc['email']} ({acc['name']})")
            t0 = time.time()
            try:
                emails = process_outlook(pw, browser, acc)
                elapsed = time.time() - t0
                if emails is not None:
                    out = os.path.join(OUTPUT_DIR, f"outlook_{acc['name'].lower()}_emails.json")
                    with open(out, 'w', encoding='utf-8') as f:
                        json.dump({'account': acc['email'], 'name': acc['name'], 'type': 'outlook',
                                   'date_range': f"{yesterday.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}",
                                   'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                                   'count': len(emails), 'emails': emails}, f, indent=2, ensure_ascii=False)
                    print(f"  ✅ {len(emails)} emails → {out} ({elapsed:.0f}s)", flush=True)
                    results[acc['email']] = f"✅ {len(emails)} emails"
                else:
                    results[acc['email']] = "❌ failed"
            except Exception as e:
                print(f"  ❌ {str(e)[:100]}", flush=True)
                results[acc['email']] = "❌ error"
            print()

        browser.close()

    total = time.time() - start_time
    print(f"{'='*60}")
    print(f"📊 RESULTS (total: {total:.0f}s)")
    print(f"{'='*60}")
    for em, st in results.items():
        print(f"  {em:45s} | {st}")
    print(f"\n📁 JSON files: {OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
