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
# decode either base64 or raw JSON
try:
    decoded = base64.b64decode(creds_env)
    creds_str = decoded.decode('utf-8')
    creds_dict = json.loads(creds_str)
except Exception:
    try:
        creds_dict = json.loads(creds_env)
    except Exception as e:
        print(f"[FATAL] Failed to parse Google service account JSON: {e}")
        sys.exit(1)

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet_key = os.environ.get('GOOGLE_SHEET_ID')
if not sheet_key:
    print("[FATAL] Missing GOOGLE_SHEET_ID env var.")
    sys.exit(1)
sheet = client.open_by_key(sheet_key).sheet1

# --- Reddit API setup ---
reddit = praw.Reddit(
    client_id=os.environ.get('REDDIT_CLIENT_ID') or os.environ.get('CLIENT_ID'),
    client_secret=os.environ.get('REDDIT_CLIENT_SECRET') or os.environ.get('CLIENT_SECRET'),
    username=os.environ.get('REDDIT_USERNAME') or os.environ.get('USERNAME'),
    password=os.environ.get('REDDIT_PASSWORD') or os.environ.get('PASSWORD'),
    user_agent='Cross-Sub Ban Bot/1.0 by ' + (os.environ.get('REDDIT_USERNAME') or os.environ.get('USERNAME'))
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

def already_listed(user):
    return any(r['Username'].lower() == user.lower() for r in sheet.get_all_records())

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
            return True
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    sheet.append_row([username,'manual','',now,'yes','',moderator,modsub,''])
    return True

# --- Modmail override check ---
def check_modmail_for_overrides():
    print("[DEBUG] Starting modmail override check...")
    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
        except Exception as e:
            print(f"[WARN] Cannot access r/{sub} modmail: {e}")
            continue
        for state in ("new","mod","all"):
            print(f"[DEBUG] Checking modmail with state '{state}'")
            try:
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
                                print(f"[OVERRIDE] {user} by {sender} in {sub}")
            except Exception as e:
                print(f"[WARN] Modmail error in r/{sub} state={state}: {e}")
                continue

# --- Sync bans ---
def sync_bans_from_sub(sub):
    print(f"--- Checking modlog for {sub}")
    try:
        sr = reddit.subreddit(sub)
        logs = sr.mod.log(action='banuser', limit=50)
        for log in logs:
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
            if user and (user.lower() in EXEMPT_USERS or is_mod(sr, user)):
                continue
            if already_logged_action(lid) or already_listed(user):
                continue
            if get_recent_sheet_entries(source) >= DAILY_BAN_LIMIT:
                continue
            row = [user,source,CROSS_SUB_BAN_REASON,ts.strftime('%Y-%m-%d %H:%M:%S'),'',lid,'','']
            sheet.append_row(row)
            print(f"[LOGGED] {user} from {source} — modlog ID: {lid}")
    except prawcore.exceptions.Forbidden:
        print(f"[WARN] Bot not a mod in r/{sub}, skipping sync")
    except prawcore.exceptions.NotFound:
        print(f"[WARN] Modlog endpoint not found for r/{sub} (bot not invited or sub missing), skipping sync")
    except Exception as e:
        print(f"[ERROR] Unexpected error syncing modlog for r/{sub}: {e}")

# --- Enforce bans ---
def enforce_bans_on_sub(sub):
    print(f"--- Enforcing bans in {sub}")
    try:
        sr = reddit.subreddit(sub)
    except Exception as e:
        print(f"[WARN] Cannot access r/{sub}, skipping enforcement: {e}")
        return
    try:
        bans = {b.name.lower(): b for b in sr.banned(limit=None)}
    except prawcore.exceptions.Forbidden:
        print(f"[WARN] Cannot list bans in r/{sub}, skipping enforcement")
        return
    except prawcore.exceptions.NotFound:
        print(f"[WARN] Ban list not found for r/{sub} (bot not invited or sub missing), skipping enforcement")
        return
    except Exception as e:
        print(f"[ERROR] Unexpected error fetching ban list for r/{sub}: {e}")
        return

    for r in sheet.get_all_records():
        user = r.get('Username','')
        src = r.get('SourceSub','')
        if not user or not src:
            continue
        ul = user.lower()
        # forgiven
        if is_forgiven(user):
            if ul in bans and CROSS_SUB_BAN_REASON.lower() in (getattr(bans[ul],'note','') or '').lower():
                try:
                    sr.banned.remove(user)
                    print(f"[UNBANNED] {user} in {sub} (forgiven)")
                except praw.exceptions.APIException as e:
                    print(f"[ERROR] Unban failed for {user} in {sub}: {e}")
                except Exception as e:
                    print(f"[ERROR] Failed to unban {user} in {sub}: {e}")
            continue
        # skip if already banned/exempt/mod
        if ul in bans or ul in EXEMPT_USERS or is_mod(sr, user):
            continue
        # ban
        try:
            sr.banned.add(user, ban_reason=CROSS_SUB_BAN_REASON, note=f"Cross-sub ban from {src}")
            print(f"[BANNED] {user} in {sub}")
        except praw.exceptions.APIException as e:
            if e.error_type == 'USER_DOESNT_EXIST':
                print(f"[WARN] Cannot ban {user} in {sub}: user doesn't exist, skipping.")
            else:
                print(f"[ERROR] Failed to ban {user} in {sub}: {e}")
        except Exception as e:
            print(f"[ERROR] Failed to ban {user} in {sub}: {e}")

# --- Main ---
if __name__=='__main__':
    check_modmail_for_overrides()
    for s in TRUSTED_SUBS:
        sync_bans_from_sub(s)
    for s in TRUSTED_SUBS:
        enforce_bans_on_sub(s)
