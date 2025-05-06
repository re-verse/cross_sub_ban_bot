from datetime import datetime
import time
from bot_config import sheet, reddit, TRUSTED_SUBS
from core_utils import is_mod  # ensure this exists

def check_modmail():
    print("[STEP] Checking for pardon and exemption messages...")
    for sub in TRUSTED_SUBS:
        print(f"[MODMAIL] Reading modmail for r/{sub}...")
        try:
            sr = reddit.subreddit(sub)
            for state in ("new", "mod"):
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

                    body_l = body.lower()
                    parts = body_l.split()
                    if body_l.startswith('/xsub pardon') and len(parts) >= 3:
                        user = parts[2].lstrip('u/').strip()
                        # Verify user was banned in this sub
                        records = sheet.get_all_records()
                        matched = next((r for r in records if r.get('Username', '').lower() == user.lower()), None)
                        if matched and matched.get('SourceSub', '').lower() == sub.lower():
                            apply_override(user, sender, sub)
                            convo.reply(body=f"✅ u/{user} has been forgiven and will not be banned.")
                        else:
                            print(f"[WARN] Mod u/{sender} tried to pardon u/{user}, but ban was not from r/{sub}")
                            convo.reply(f"⚠️ Can't pardon u/{user} — they were not banned in r/{sub}.")

                    elif body_l.startswith('/xsub exempt') and len(parts) >= 3:
                        user = parts[2].lstrip('u/').strip()
                        if apply_exemption(user, sub):
                            convo.reply(body=f"✅ u/{user} has been exempted from bans in r/{sub}.")
        except Exception as e:
            print(f"[WARN] Could not check modmail for r/{sub}: {e}")
        time.sleep(2)  # Throttle to avoid hitting 429

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
