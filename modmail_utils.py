from datetime import datetime
from bot_config import sheet, reddit, TRUSTED_SUBS

from core_utils import is_mod  # make sure this exists in a new utils file

def check_modmail():
    print("[STEP] Checking for pardon and exemption messages...")
    for sub in TRUSTED_SUBS:
        print(f"[MODMAIL] Scanning modmail in r/{sub}")
        try:
            sr = reddit.subreddit(sub)
            for state in ("new", "mod"):
                print(f"[MODMAIL] Fetching '{state}' conversations for r/{sub}")
                for convo in sr.modmail.conversations(state=state):
                    if not convo.messages:
                        continue
                    last = convo.messages[-1]
                    body = getattr(last, 'body_markdown', '').strip()
                    sender = getattr(last.author, 'name', '').lower()
                    if not sender or not body:
                        continue
                    if not is_mod(sr, sender):
                        continue

                    print(f"[MODMAIL] Last message from u/{sender}: '{body[:40]}...'")

                    if body.lower().startswith('/xsub pardon'):
                        parts = body.split()
                        if len(parts) >= 3:
                            user = parts[2].lstrip('u/').strip()
                            print(f"[MODMAIL] Applying pardon override for u/{user} in r/{sub}")
                            apply_override(user, sender, sub)
                            convo.reply(body=f"✅ u/{user} has been forgiven and will not be banned.")
                    elif body.lower().startswith('/xsub exempt'):
                        parts = body.split()
                        if len(parts) >= 3:
                            user = parts[2].lstrip('u/').strip()
                            print(f"[MODMAIL] Applying exemption override for u/{user} in r/{sub}")
                            if apply_exemption(user, sub):
                                convo.reply(body=f"✅ u/{user} has been exempted from bans in r/{sub}.")
        except Exception as e:
            print(f"[WARN] Modmail check failed for r/{sub}: {e}")
            continue
            
def apply_override(username, moderator, modsub):
    records = sheet.get_all_records()
    for i, r in enumerate(records, start=2):
        if r.get('Username', '').lower() == username.lower():
            sheet.update_cell(i, 5, 'yes')
            sheet.update_cell(i, 7, moderator)
            sheet.update_cell(i, 8, modsub)
            return True
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    sheet.append_row([username, 'manual', '', now, 'yes', '', moderator, modsub, ''])
    return True

def apply_exemption(username, modsub):
    records = sheet.get_all_records()
    for i, r in enumerate(records, start=2):
        if r.get('Username', '').lower() == username.lower():
            current = str(r.get('ExemptSubs', '')).lower()
            parts = {p.strip() for p in current.split(',') if p.strip()}
            parts.add(modsub.lower())
            new_field = ', '.join(sorted(parts))
            sheet.update_cell(i, 10, new_field)
            return True
    return False
