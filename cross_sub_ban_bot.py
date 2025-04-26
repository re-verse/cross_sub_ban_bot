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

def forgiveness_info(user):
    for r in sheet.get_all_records():
        if r.get('Username','').lower() == user.lower():
            return str(r.get('ManualOverride','')).lower(), str(r.get('ForgiveTimestamp',''))
    return '', ''

def apply_override(username, moderator, modsub):
    records = sheet.get_all_records()
    for i,r in enumerate(records, start=2):
        if r.get('Username','').lower() == username.lower():
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            sheet.update_cell(i,5,'yes')
            sheet.update_cell(i,7,moderator)
            sheet.update_cell(i,8,modsub)
            sheet.update_cell(i,9,now)
            return True
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    sheet.append_row([username,'manual','',now,'yes','',moderator,modsub,now])
    return True

def clear_override(username):
    records = sheet.get_all_records()
    for i,r in enumerate(records, start=2):
        if r.get('Username','').lower() == username.lower():
            sheet.update_cell(i,5,'')
            sheet.update_cell(i,7,'')
            sheet.update_cell(i,8,'')
            sheet.update_cell(i,9,'')
            return True
    return False

# (The rest of the bot logic will continue in the next block to avoid cutoff)
