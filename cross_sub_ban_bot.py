#!/usr/bin/env python3

import json
import base64
import os
import sys
import praw
import prawcore
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

# --- Load configuration ---
with open("config.json") as config_file:
    config = json.load(config_file)

CROSS_SUB_BAN_REASON = config.get("CROSS_SUB_BAN_REASON", "Auto XSub Pact Ban")
EXEMPT_USERS = set(u.lower() for u in config.get("EXEMPT_USERS", []))
DAILY_BAN_LIMIT = config.get("DAILY_BAN_LIMIT", 30)
MAX_LOG_AGE_MINUTES = config.get("MAX_LOG_AGE_MINUTES", 60)
ROW_RETENTION_DAYS = config.get("ROW_RETENTION_DAYS", 30)

# --- Public log files ---
PUBLIC_LOG_JSON = "public_ban_log.json"
PUBLIC_LOG_MD = "public_ban_log.md"

# --- Trusted subreddits ---
def load_trusted_subs(path="trusted_subs.txt"):
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]

TRUSTED_SUBS = load_trusted_subs()
TRUSTED_SOURCES = {f"r/{sub}" for sub in TRUSTED_SUBS}

# --- Google Sheets setup ---
creds_env = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
if not creds_env:
    print("[FATAL] Missing GOOGLE_SERVICE_ACCOUNT_JSON env var.")
    sys.exit(1)
try:
    decoded = base64.b64decode(creds_env)
    creds_str = decoded.decode('utf-8')
    creds_dict = json.loads(creds_str)
except Exception:
    try:
        creds_dict = json.loads(creds_env)
    except Exception as e:
        print(f"[FATAL] Failed to parse service account JSON: {e}")
        sys.exit(1)

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet_key = os.environ.get('GOOGLE_SHEET_ID')
if not sheet_key:
    print("[FATAL] Missing GOOGLE_SHEET_ID env var.")
    sys.exit(1)
sheet = client.open_by_key(sheet_key).sheet1
print(f"[INFO] Google Sheet '{sheet_key}' opened, worksheet '{sheet.title}' loaded.")

# --- Reddit API setup ---
reddit = praw.Reddit(
    client_id=os.environ.get('REDDIT_CLIENT_ID') or os.environ.get('CLIENT_ID'),
    client_secret=os.environ.get('REDDIT_CLIENT_SECRET') or os.environ.get('CLIENT_SECRET'),
    username=os.environ.get('REDDIT_USERNAME') or os.environ.get('USERNAME'),
    password=os.environ.get('REDDIT_PASSWORD') or os.environ.get('PASSWORD'),
    user_agent='Cross-Sub Ban Bot/1.0'
)

# --- Caches ---
mod_cache = {}

# --- Helpers ---
def is_mod(subreddit, user):
    sub = subreddit.display_name.lower()
    if sub not in mod_cache:
        try:
            mod_cache[sub] = {m.name.lower() for m in subreddit.moderator()}
        except Exception:
            mod_cache[sub] = set()
    return user.lower() in mod_cache[sub]

def already_logged_action(log_id):
    return log_id in sheet.col_values(6)

def get_recent_sheet_entries(source_sub):
    cutoff = datetime.utcnow() - timedelta(days=1)
    count = 0
    for r in sheet.get_all_records():
        if r.get('SourceSub') == source_sub:
            ts = r.get('Timestamp')
            try:
                t = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
                if t > cutoff:
                    count += 1
            except:
                pass
    return count

def forgiveness_timestamp(user):
    for r in sheet.get_all_records():
        if r.get('Username','').lower() == user.lower():
            ts = r.get('ForgiveTimestamp','')
            if ts:
                try:
                    return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
                except:
                    return None
    return None

def is_forgiven(user):
    for r in sheet.get_all_records():
        if r.get('Username','').lower() == user.lower() and str(r.get('ManualOverride','')).lower() in ('yes','true'):
            return True
    return False

def apply_override(username, moderator, modsub):
    records = sheet.get_all_records()
    for i,r in enumerate(records, start=2):
        if r.get('Username','').lower() == username.lower():
            sheet.update_cell(i,5,'yes')
            sheet.update_cell(i,7,moderator)
            sheet.update_cell(i,8,modsub)
            sheet.update_cell(i,9,datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
            return True
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    sheet.append_row([username,'manual','',now,'yes','',moderator,modsub,now])
    return True

def log_public_action(action, username, subreddit, source_sub="", actor="", note=""):
    entry = {
        "timestamp": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        "action": action,
        "username": username,
        "subreddit": subreddit,
        "source_sub": source_sub,
        "actor": actor,
        "note": note
    }
    try:
        if os.path.exists(PUBLIC_LOG_JSON):
            with open(PUBLIC_LOG_JSON,'r') as f:
                data = json.load(f)
        else:
            data = []
        data.append(entry)
        with open(PUBLIC_LOG_JSON,'w') as f:
            json.dump(data, f, indent=2)
        with open(PUBLIC_LOG_MD,'a') as f:
            f.write(f"### [{entry['timestamp']}] {'✅' if action=='UNBANNED' else '❌'} {action} u/{username}\n")
            f.write(f"- **Subreddit**: r/{subreddit}\n")
            if source_sub:
                f.write(f"- **Source Sub**: {source_sub}\n")
            if actor:
                f.write(f"- **Actor**: {actor}\n")
            if note:
                f.write(f"- **Note**: {note}\n")
            f.write("\n")
    except Exception as e:
        print(f"[ERROR] Failed to write public log: {e}")

# --- Modmail override check ---
def check_modmail_for_overrides():
    print("[STEP] Checking for pardon messages...")
    for sub in TRUSTED_SUBS:
        print(f"[INFO] Reading modmail in r/{sub}")
        try:
            sr = reddit.subreddit(sub)
            for state in ("new","mod","all"):
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
                    if body.lower().startswith('/xsub pardon'):
                        parts = body.split()
                        if len(parts)>=3:
                            user = parts[2].lstrip('u/').strip()
                            if apply_override(user, sender, sub):
                                convo.reply(body=f"✅ u/{user} has been forgiven and will not be banned.")
        except Exception:
            continue

# --- Sync bans from modlog ---
def sync_bans_from_sub(sub):
    print(f"[STEP] Checking modlog for r/{sub}")
    try:
        sr = reddit.subreddit(sub)
        for log in sr.mod.log(action='banuser', limit=50):
            user = log.target_author
            source = f"r/{log.subreddit}"
            lid = log.id
            ts = datetime.utcfromtimestamp(log.created_utc)
            if datetime.utcnow()-ts > timedelta(minutes=MAX_LOG_AGE_MINUTES):
                continue
            if (log.description or '').strip().lower() != CROSS_SUB_BAN_REASON.lower():
                continue
            if source not in TRUSTED_SOURCES:
                continue
            forgive_time = forgiveness_timestamp(user)
            if forgive_time and ts > forgive_time + timedelta(minutes=60):
                apply_override(user, '', '')
                continue
            if user and (user.lower() in EXEMPT_USERS or is_mod(sr, user)):
                continue
            if already_logged_action(lid):
                continue
            if get_recent_sheet_entries(source) >= DAILY_BAN_LIMIT:
                continue
            sheet.append_row([user,source,CROSS_SUB_BAN_REASON,ts.strftime('%Y-%m-%d %H:%M:%S'),'',lid,'',''])
    except (prawcore.exceptions.Forbidden, prawcore.exceptions.NotFound):
        print(f"[WARN] Cannot access modlog for r/{sub}, skipping.")

# --- Enforce bans/unbans ---
def enforce_bans_on_sub(sub):
    print(f"[STEP] Enforcing bans/unbans in r/{sub}")
    try:
        sr = reddit.subreddit(sub)
        bans = {b.name.lower(): b for b in sr.banned(limit=None)}
    except Exception:
        print(f"[WARN] Cannot fetch ban list for r/{sub}, skipping.")
        return
    cutoff = datetime.utcnow() - timedelta(days=ROW_RETENTION_DAYS)
    records = []
    all_rows = sheet.get_all_records()
    for r in all_rows:
        ts = r.get('Timestamp','')
        try:
            t = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
            if t > cutoff:
                records.append(r)
        except:
            continue

    # --- FIXED DELETION LOGIC ---
    now = datetime.utcnow()
    to_delete = []
    for idx, r in enumerate(all_rows, start=2):
        marker = str(r.get('ForgiveTimestamp','')).strip()
        if marker.endswith('deleted'):
            try:
                mark_time = datetime.strptime(marker.replace(' deleted',''), '%Y-%m-%d %H:%M:%S')
                if now - mark_time > timedelta(hours=24):
                    to_delete.append(idx)
            except:
                continue
    for idx in reversed(to_delete):
        sheet.delete_row(idx)
        print(f"[INFO] Removed old deleted user at row {idx}.")

    any_action = False
    for r in records:
        user = r.get('Username','')
        src = r.get('SourceSub','')
        if not user or not src:
            continue
        ul = user.lower()
        deleted_marker = str(r.get('ForgiveTimestamp','')).strip()
        if deleted_marker:
            print(f"[SKIP] {user} already marked deleted in sheet")
            continue
        if is_forgiven(user):
            if ul in bans and CROSS_SUB_BAN_REASON.lower() in (getattr(bans[ul],'note','') or '').lower():
                try:
                    sr.banned.remove(user)
                    print(f"[UNBANNED] Forgiven u/{user} in r/{sub}")
                    log_public_action("UNBANNED", user, sub, src, "Bot", "Forgiven override")
                    any_action = True
                except Exception:
                    pass
            continue
        if ul in bans or ul in EXEMPT_USERS or is_mod(sr, user):
            continue
        try:
            sr.banned.add(user, ban_reason=CROSS_SUB_BAN_REASON, note=f"Cross-sub ban from {src}")
            print(f"[BANNED] u/{user} in r/{sub} from {src}")
            log_public_action("BANNED", user, sub, src, "Bot", "")
            any_action = True
        except praw.exceptions.APIException as e:
            err = getattr(e._raw, 'error_type', '')
            if err == 'USER_DOESNT_EXIST':
                for idx,row in enumerate(sheet.get_all_records(), start=2):
                    if row.get('Username','').lower() == ul:
                        sheet.update_cell(idx,9,datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + ' deleted')
                        print(f"[INFO] Marked u/{user} as deleted in sheet, skipping future attempts.")
                        break
    if not any_action:
        print(f"[INFO] No bans or unbans needed in r/{sub}.")

# --- Main ---
if __name__=='__main__':
    print("=== Running Cross-Sub Ban Bot ===")
    check_modmail_for_overrides()
    for s in TRUSTED_SUBS:
        sync_bans_from_sub(s)
    for s in TRUSTED_SUBS:
        enforce_bans_on_sub(s)
    print("=== Bot run complete ===")
