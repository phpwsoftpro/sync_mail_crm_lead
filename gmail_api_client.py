import os
import base64
from email.message import EmailMessage
from google.oauth2 import service_account
from googleapiclient.discovery import build
import socket
# 2026-09-21 — smart_mail_daemon hung for 3 days on a Gmail socket Google had already closed
# (CLOSE_WAIT): googleapiclient/httplib2 has NO timeout unless the process sets one, so a dropped
# connection blocks forever and launchd (which never restarts a "running" job) keeps it that way.
# One process-wide default covers every service built here.
socket.setdefaulttimeout(120)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_DIR = os.path.join(SCRIPT_DIR, 'credentials')

WSOFTPRO_JSON = os.path.join(CREDENTIALS_DIR, 'wsoftpro-sync-c80f12bfa6f4.json')
INTERSTELLARS_JSON = os.path.join(CREDENTIALS_DIR, 'wsoftpro-sync-504810-c94d4b64177e.json')

SCOPES = ['https://mail.google.com/']

def get_credential_file(email_address):
    """Determine which JSON file to use based on email domain."""
    domain = email_address.split('@')[-1].lower()
    if domain == 'interstellarsagency.com':
        return INTERSTELLARS_JSON
    # default to WSoftPro for wsoftpro.com and hyperspacedev.com
    return WSOFTPRO_JSON

def get_gmail_service(email_address):
    """Initialize and return a Gmail API service instance."""
    json_file = get_credential_file(email_address)
    
    if not os.path.exists(json_file):
        raise FileNotFoundError(f"Credential file not found: {json_file}")
        
    creds = service_account.Credentials.from_service_account_file(
        json_file, scopes=SCOPES)
    
    # Impersonate the specific user
    delegated_creds = creds.with_subject(email_address)
    
    service = build('gmail', 'v1', credentials=delegated_creds)
    return service

def search_threads(service, query, max_results=5):
    """Search for threads matching a query."""
    results = service.users().threads().list(userId='me', q=query, maxResults=max_results).execute()
    return results.get('threads', [])

def search_thread_id(service, query):
    """Return the first thread ID matching the query."""
    threads = search_threads(service, query, max_results=1)
    if threads:
        return threads[0]['id']
    return None

def fetch_recent_emails(service, max_results=50):
    """Fetch recent emails from the inbox with their content."""
    messages = []
    page_token = None
    
    while len(messages) < max_results:
        # Google caps maxResults at 500
        fetch_count = min(500, max_results - len(messages))
        results = service.users().messages().list(
            userId='me', q='in:inbox', maxResults=fetch_count, pageToken=page_token
        ).execute()
        
        batch = results.get('messages', [])
        if not batch:
            break
            
        messages.extend(batch)
        page_token = results.get('nextPageToken')
        if not page_token:
            break
            
    # Cap exactly at max_results if it over-fetched
    messages = messages[:max_results]
    
    email_data_list = []
    print(f"   📥 Downloading content for {len(messages)} messages...", flush=True)
    
    for i, msg_meta in enumerate(messages):
        if i % 100 == 0 and i > 0:
            print(f"      ... downloaded {i}/{len(messages)}", flush=True)
            
        msg = service.users().messages().get(userId='me', id=msg_meta['id'], format='full').execute()
        
        headers = msg['payload'].get('headers', [])
        subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')
        from_str = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
        to_str = next((h['value'] for h in headers if h['name'].lower() == 'to'), '')
        date_str = next((h['value'] for h in headers if h['name'].lower() == 'date'), '')
        message_id = next((h['value'] for h in headers if h['name'].lower() == 'message-id'), '')
        
        # Extract body recursively
        def get_body_recursive(part, mime_type):
            if part.get('mimeType') == mime_type:
                data = part['body'].get('data')
                if data:
                    return base64.urlsafe_b64decode(data).decode('utf-8', 'ignore')
            if 'parts' in part:
                for subpart in part['parts']:
                    result = get_body_recursive(subpart, mime_type)
                    if result:
                        return result
            return None
            
        body = get_body_recursive(msg['payload'], 'text/plain')
        if not body:
            body = get_body_recursive(msg['payload'], 'text/html') or ''
                
        email_data = {
            'id': msg['id'],
            'thread_id': msg['threadId'],
            'subject': subject,
            'from': from_str,
            'to': to_str,
            'date': date_str,
            'message_id': message_id,
            'body_text': body[:5000] # Limit size
        }
        email_data_list.append(email_data)
        
    return email_data_list

def send_reply(service, thread_id, to_email, subject, body_html, from_email):
    """Send a reply to an existing thread."""
    # First, fetch the thread to get the Message-ID of the last message
    thread = service.users().threads().get(userId='me', id=thread_id).execute()
    messages = thread.get('messages', [])
    if not messages:
        raise ValueError(f"No messages found in thread {thread_id}")
    
    last_message = messages[-1]
    headers = last_message['payload'].get('headers', [])
    
    message_id = None
    references = None
    for h in headers:
        if h['name'].lower() == 'message-id':
            message_id = h['value']
        if h['name'].lower() == 'references':
            references = h['value']
            
    if not references and message_id:
        references = message_id
    elif message_id:
        references = f"{references} {message_id}"
        
    # Create the email message
    message = EmailMessage()
    message.set_content("Please enable HTML to view this email.")
    message.add_alternative(body_html, subtype='html')
    
    message['To'] = to_email
    message['From'] = from_email
    
    # Important for threading
    if message_id:
        message['In-Reply-To'] = message_id
    if references:
        message['References'] = references
        
    # Gmail API requires Subject to be prefixed with 'Re: ' if not already
    clean_subj = subject
    if not clean_subj.lower().startswith('re:'):
        clean_subj = f"Re: {subject}"
    message['Subject'] = clean_subj
    
    # Encode message
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    
    create_message = {
        'raw': encoded_message,
        'threadId': thread_id
    }
    
    # Send
    send_message = service.users().messages().send(userId='me', body=create_message).execute()
    return send_message
