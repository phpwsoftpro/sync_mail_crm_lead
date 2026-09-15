#!/usr/bin/env python3
"""rescue_replies.py — the "Bot Cứu Hộ": move tickets back to Reply Client (3) when the client has
replied but the ticket is stuck in a dormant stage. Replaces the dead crm_daemon.py rescue role.

Why needed: Odoo fetchmail (create_uid=1) posts incoming client emails into the lead chatter but does
NOT change the stage. Without a rescue bot those replies sit unseen in Done Follow Up / Old Lead.

Rescue rule (per workflow #462027 §3): a ticket in a dormant stage whose LATEST inbound email — from a
real client (not our own domains, not a system/no-reply/bounce/auto-reply sender) — is newer than the
ticket's last stage change → move to Reply Client (3), type=opportunity, keep the source tag, post a note.

  ./venv/bin/python rescue_replies.py            # dry-run: list what WOULD move
  ./venv/bin/python rescue_replies.py --apply    # actually move
"""
import sys
sys.path.append("/Users/trung/syncmail-repo-auto")
import smart_mail_daemon as d

DORMANT_STAGES = [9, 10, 34, 35, 19]      # Done Follow Up 1/2, Old Lead, Send Email Done, Unable to Send
REPLY_CLIENT = 3
APPLY = "--apply" in sys.argv

def rpc(sess, model, method, args, kwargs=None):
    r = sess.post(f"{d.ODOO_CRM_URL}/web/dataset/call_kw/{model}/{method}",
                  json={"jsonrpc": "2.0", "method": "call",
                        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs or {}}},
                  timeout=30).json()
    if r.get("error"):
        raise RuntimeError(str(r["error"].get("data", {}).get("message", r["error"]))[:160])
    return r.get("result")

def main():
    sess = d.get_crm_session()
    leads = rpc(sess, "crm.lead", "search_read", [[["active", "=", True], ["stage_id", "in", DORMANT_STAGES]]],
                {"fields": ["id", "name", "email_from", "stage_id", "date_last_stage_update"], "limit": 100000})
    moved = skipped_junk = skipped_nonew = 0
    to_move = []
    for l in leads:
        # latest inbound email on this lead
        msgs = rpc(sess, "mail.message", "search_read",
                   [[["model", "=", "crm.lead"], ["res_id", "=", l["id"]], ["message_type", "=", "email"]]],
                   {"fields": ["date", "email_from", "subject", "body"], "order": "date desc", "limit": 1})
        if not msgs:
            skipped_nonew += 1; continue
        m = msgs[0]
        frm = (m.get("email_from") or "").lower()
        # must be FROM the client, not us
        if any(dom in frm for dom in ("wsoftpro.com", "hyperspacedev.com", "interstellarsagency.com", "musubiit.com")):
            skipped_nonew += 1; continue
        # must be newer than the last stage change (i.e. arrived after we parked it)
        if not (m["date"] and l["date_last_stage_update"] and m["date"] > l["date_last_stage_update"]):
            skipped_nonew += 1; continue
        # must be a genuine human reply, not junk (OOO/bounce/auto-ack/newsletter/system sender)
        addr = frm.split("<")[-1].split(">")[0] if "<" in frm else frm
        text = d.clean_html_body(m.get("body") or "")
        lab = d.fallback_classify(m.get("email_from", ""), m.get("subject", ""), text[:1500])
        if d.is_system_sender(addr) or lab["stage"] in d.JUNK_STAGE_KEYS:
            skipped_junk += 1; continue
        to_move.append((l, m))

    print(f"{len(leads)} tickets in dormant stages; {len(to_move)} genuine client replies to rescue "
          f"(skipped {skipped_junk} junk/system, {skipped_nonew} no-new-client-mail)", flush=True)
    for l, m in to_move:
        cur = (l["stage_id"] or [None, "?"])[1]
        print(f"  #{l['id']:6} {cur:24} <- {(m.get('email_from') or '')[:38]:38} | {l['name'][:40]}", flush=True)
        if APPLY:
            try:
                rpc(sess, "crm.lead", "write", [[l["id"]], {"stage_id": REPLY_CLIENT, "type": "opportunity"}])
                rpc(sess, "crm.lead", "message_post", [[l["id"]]],
                    {"body": "🤖 Bot Cứu Hộ: khách vừa rep → chuyển về Reply Client.",
                     "message_type": "comment", "subtype_xmlid": "mail.mt_note"})
                moved += 1
            except Exception as e:
                print(f"     FAIL: {e}", flush=True)
    print(("APPLIED, moved %d" % moved) if APPLY else "DRY-RUN (chạy --apply để thực hiện)", flush=True)

if __name__ == "__main__":
    main()
