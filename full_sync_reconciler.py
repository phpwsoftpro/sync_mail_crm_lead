import sys
import os
import requests
from datetime import datetime
import re
import xmlrpc.client
import ssl
import json

import gmail_api_client
# Reuse the daemon's classifiers so this script can never create a lead the daemon would
# have thrown in Mail Rác (before 2026-09-16 it created one for EVERY unknown sender —
# newsletters, DMARC reports, mailer-daemon, no-reply… — straight into New).
from smart_mail_daemon import (is_system_sender, fallback_classify, JUNK_STAGE_KEYS,
                               tag_for_account)

OUR_DOMAINS = ('wsoftpro.com', 'hyperspacedev.com', 'interstellarsagency.com', 'musubiit.com')

ACCOUNTS = [
    'robert@wsoftpro.com',
    'vanessa@wsoftpro.com',
    'supportteam@wsoftpro.com',
    'jennifer@hyperspacedev.com',
    'luna@hyperspacedev.com',
    'helen@interstellarsagency.com',
    'yuna@musubiit.com'
]

CRM_URL = 'https://crm.wsoftpro.com'
PAYROLL_URL = 'https://payroll.wsoftpro.com'
CRM_DB = '27_05'
PAYROLL_DB = '29_5'
# Credentials come from .env (requirement #7: no plaintext passwords in code).
CRM_USER = os.environ.get('CRM_USER') or os.environ.get('ODOO_LOGIN') or ''
CRM_PASS = os.environ.get('CRM_PASSWORD') or os.environ.get('ODOO_PASSWORD') or ''
if not CRM_USER or not CRM_PASS:
    raise SystemExit('full_sync_reconciler: CRM_USER / CRM_PASSWORD missing in .env')

PROCESSED_FILE = '/Users/trung/syncmail-repo-auto/.full_sync_processed.json'

def load_processed():
    try:
        if os.path.exists(PROCESSED_FILE):
            with open(PROCESSED_FILE, 'r') as f:
                return set(json.load(f))
    except Exception as e:
        print(f"Error loading processed file: {e}")
    return set()

def save_processed(processed_set):
    with open(PROCESSED_FILE, 'w') as f:
        json.dump(list(processed_set), f)

def extract_email_address(from_str):
    match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', from_str or '')
    return match.group(0).lower().strip() if match else ''

def extract_sender_name(from_str):
    match = re.match(r'^([^<]+)<', from_str or '')
    if match:
        return match.group(1).strip().strip('"\'')
    return from_str.strip() if from_str else ''

def main():
    print("Starting Reconciliation...")
    processed_set = load_processed()
    
    # 1. Fetch CRM leads (ALL)
    print("Fetching CRM leads...")
    requests.packages.urllib3.disable_warnings()
    s = requests.Session()
    s.verify = False
    
    auth_res = s.post(f'{CRM_URL}/web/session/authenticate', json={
        'jsonrpc': '2.0', 'method': 'call',
        'params': {'db': CRM_DB, 'login': CRM_USER, 'password': CRM_PASS}
    })
    
    res = s.post(f'{CRM_URL}/web/dataset/call_kw/crm.lead/search_read', json={
        'jsonrpc': '2.0', 'method': 'call',
        'params': {
            'model': 'crm.lead', 'method': 'search_read',
            'args': [[]],
            'kwargs': {'fields': ['id', 'name', 'email_from'], 'limit': False}
        }
    })
    
    crm_leads = res.json().get('result', [])
    crm_emails = set([extract_email_address(lead.get('email_from', '')) for lead in crm_leads if lead.get('email_from')])
    print(f"Found {len(crm_leads)} CRM leads.")
    
    # 2. Fetch Gmail emails
    total_emails_scanned = 0
    total_already_synced = 0
    remaining_to_sync = 0
    
    new_to_process = []
    
    for account in ACCOUNTS:
        print(f"Processing account: {account}")
        try:
            service = gmail_api_client.get_gmail_service(account)
            
            messages = []
            page_token = None
            while True:
                results = service.users().messages().list(userId='me', q='', maxResults=500, pageToken=page_token).execute()
                batch = results.get('messages', [])
                if not batch:
                    break
                messages.extend(batch)
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
                    
            print(f"  Found {len(messages)} messages for {account}")
            total_emails_scanned += len(messages)
            
            for msg_meta in messages:
                msg_id = msg_meta['id']
                if msg_id in processed_set:
                    total_already_synced += 1
                else:
                    new_to_process.append((account, msg_id, service))
                    
        except Exception as e:
            print(f"Error processing {account}: {e}")
            
    remaining_to_sync = len(new_to_process)
    print(f"Total emails scanned (all accounts): {total_emails_scanned}")
    print(f"Already processed: {total_already_synced}")
    print(f"Remaining to process: {remaining_to_sync}")
    
    # Process up to 50 unprocessed emails
    batch_to_process = new_to_process[:360]
    
    created_leads = []
    skipped_junk = []

    for account, msg_id, service in batch_to_process:
        try:
            msg = service.users().messages().get(userId='me', id=msg_id, format='metadata', metadataHeaders=['From', 'Subject', 'Date']).execute()
            
            headers = msg['payload'].get('headers', [])
            from_str = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
            subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')
            snippet = msg.get('snippet', '')
            
            sender_email = extract_email_address(from_str)
            sender_name = extract_sender_name(from_str)
            
            processed_set.add(msg_id)
            
            if not sender_email:
                continue

            # --- Gates added 2026-09-16 (this script used to create a lead for EVERY unknown
            # sender in the whole mailbox history — that is where the junk leads came from) ---
            if any(sender_email.endswith(d) for d in OUR_DOMAINS):
                continue                                   # our own personas mailing each other
            if is_system_sender(sender_email):
                continue                                   # no-reply / DMARC / mailer-daemon / notifications
            label = fallback_classify(from_str, subject, snippet)
            if label.get('stage') in JUNK_STAGE_KEYS:
                skipped_junk.append(sender_email)
                continue                                   # OOO, auto-ack, bounces, newsletters, job spam…

            if sender_email not in crm_emails:
                print(f"Creating lead for {sender_email}...")
                # Requirement #8: anything the team must see is type='opportunity' (the Kanban
                # only shows opportunities) and requirement #6: exactly one source tag.
                lead_vals = {
                    'name': subject[:100] if subject else 'No Subject',
                    'email_from': sender_email,
                    'contact_name': sender_name,
                    'description': snippet[:1000] if snippet else '',
                    'stage_id': 1,
                    'type': 'opportunity',
                }
                tag_id = tag_for_account(account)
                if tag_id:
                    lead_vals['tag_ids'] = [(4, tag_id)]
                create_res = s.post(f'{CRM_URL}/web/dataset/call_kw/crm.lead/create', json={
                    'jsonrpc': '2.0', 'method': 'call',
                    'params': {
                        'model': 'crm.lead', 'method': 'create',
                        'args': [lead_vals],
                        'kwargs': {"context": {"mail_create_nosubscribe": True, "mail_notrack": True, "tracking_disable": True}}
                    }
                })
                
                new_id = create_res.json().get('result')
                created_leads.append({
                    'sender_email': sender_email,
                    'subject': subject,
                    'new_id': new_id
                })
                # Add to crm_emails to avoid duplicate creation in same run
                crm_emails.add(sender_email)
                
        except Exception as e:
            print(f"Error fetching/creating msg_id {msg_id}: {e}")
            
    save_processed(processed_set)
    
    # 4. Post report to Odoo Mail Reports (Channel 188)
    print("Posting report to Odoo...")
    now_str = datetime.now().strftime('%H:%M %d/%m')
    report_html = f"<p>🤖 <b>Bot Report: 🔄 Full Sync Report ({now_str})</b></p>\n"
    report_html += f"<p>📊 Tổng hợp đồng bộ Gmail ↔ CRM:</p>\n"
    report_html += f"<p>📧 Tổng email trên Gmail: {total_emails_scanned}</p>\n"
    report_html += f"<p>✅ Đã sync: {total_already_synced}</p>\n"
    report_html += f"<p>🆕 Sync mới lần này: {len(batch_to_process)}</p>\n"
    report_html += f"<p>⏳ Còn lại: {max(0, remaining_to_sync - len(batch_to_process))}</p>\n"
    report_html += f"<br/>\n"
    
    report_html += f"<p>🆕 <b>Leads mới tạo:</b></p>\n"
    if len(created_leads) == 0:
        report_html += "<p>Không có leads mới nào được tạo trong đợt này.</p>\n"
    else:
        for l in created_leads:
            new_id = l.get('new_id')
            subject = l['subject'][:40] + ('...' if len(l['subject']) > 40 else '')
            report_html += f"<p>• {l['sender_email']} — \"{subject}\" → <a href=\"https://crm.wsoftpro.com/web#id={new_id}&model=crm.lead&view_type=form\">#{new_id}</a></p>\n"
    report_html += "<br/>\n<p>👉 Hệ thống tiếp tục sync batch tiếp trong 1h nữa.</p>\n"
        
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        common = xmlrpc.client.ServerProxy(f'{PAYROLL_URL}/xmlrpc/2/common', context=ctx)
        uid = common.authenticate(PAYROLL_DB, CRM_USER, CRM_PASS, {})
        models = xmlrpc.client.ServerProxy(f'{PAYROLL_URL}/xmlrpc/2/object', context=ctx)
        try:
            models.execute_kw(PAYROLL_DB, uid, CRM_PASS, 'mail.channel', 'message_post',
                [188],
                {'body': report_html, 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment'}
            )
        except xmlrpc.client.Fault as e:
            if 'cannot marshal <class' not in str(e):
                raise e
        print("Report posted successfully.")
    except Exception as e:
        print(f"Error posting report: {e}")

if __name__ == '__main__':
    main()
