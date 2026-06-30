# syncmail

Sync emails from multiple Gmail & Outlook accounts and compare with Odoo CRM pipeline.

## Features
- **Email Sync**: Fetches emails from 4 Gmail + 6 Outlook accounts using Playwright
- **Auto Re-login**: Checks cookies first — only re-logins if session expired (visible browser for 2FA)
- **CRM Comparison**: Matches Gmail emails against Odoo CRM leads by sender email, name, subject, domain
- **Auto Ticket Creation**: Creates CRM leads for unmatched emails
- **JSON Output**: Saves each account's emails as structured JSON

## Setup

```bash
# Install dependencies
pip install playwright requests
playwright install chromium

# First run (will need manual login for each account)
python3 fetch_emails_fast.py
```

## Usage

```bash
# Sync all emails + compare with CRM (report only)
syncmail

# Sync + auto-create missing CRM tickets
syncmail --create

# Or run scripts directly:
python3 fetch_emails_fast.py      # Sync emails only
python3 compare_crm.py            # Compare with CRM only
python3 compare_crm.py --create   # Compare + create tickets
```

## Accounts

### Gmail (4)
| Account | Name |
|---------|------|
| robert@wsoftpro.com | Robert |
| vanessa@wsoftpro.com | Vanessa |
| luna@hyperspacedev.com | Luna |
| helen@interstellarsagency.com | Helen |

### Outlook (6)
| Account | Name |
|---------|------|
| travisngx135@outlook.com | Travis |
| timmy_dao@outlook.com | Timmy |
| claytonng159@outlook.com | Clayton |
| kieuvt169@outlook.com | Kieu |
| petertramvn22@outlook.com | Peter |
| ruslandevvn123@outlook.com | Ruslan |

## Output

JSON files saved to `emails/` directory:
```
emails/
├── gmail_robert_emails.json
├── gmail_vanessa_emails.json
├── gmail_luna_emails.json
├── gmail_helen_emails.json
├── outlook_travis_emails.json
├── outlook_timmy_emails.json
├── outlook_clayton_emails.json
├── outlook_kieu_emails.json
├── outlook_peter_emails.json
└── outlook_ruslan_emails.json
```

CRM comparison report: `crm_email_comparison.json`
