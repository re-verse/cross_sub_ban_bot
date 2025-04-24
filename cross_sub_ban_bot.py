import praw
import gspread
import os
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import base64
import json

# --- CONFIG ---

CROSS_SUB_BAN_REASON = "Auto XSub Pact Ban"
EXEMPT_USERS = {"AutoModerator", "xsub-pact-bot"}
DAILY_BAN_LIMIT = 10

# --- Load trusted subreddits ---

def load_trusted_subs(file_path="trusted_subs.txt"):
    with open(file_path, "r") as f:
        return [line.strip() for line in f if line.strip()]

TRUSTED_SUBS = load_trusted_subs()
TRUSTED_SOURCES = {"r/" + sub for sub in TRUSTED_SUBS}

# --- Google Sheets setup ---

creds_json = base64.b64decode(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
creds_dict = json.loads(creds_json)

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.environ['GOOGLE_SHEET_ID']).sheet1

# --- Reddit API setup ---

reddit = praw.Reddit(
    client_id=os.environ['CLIENT_ID'],
    client_secret=os.environ['CLIENT_SECRET'],
    username=os.environ['USERNAME'],
    password=os.environ['PASSWORD'],
    user_agent='NHL Cross-Sub Ban Bot'
)

# --- Caching mod lists for performance ---
mod_cache = {}

def is_mod(subreddit, user):
    subname = subreddit.display_name.lower()
    if subname not in mod_cache:
        mod_cache[subname] = {mod.name.lower() for mod in subreddit.moderator()}
    return user.lower() in mod_cache[subname]

# --- Sheet helpers ---

def get_recent_sheet_entries(source_sub):
    now = datetime.utcnow()
    return sum(1 for row in sheet.get_all_records()
               if row['SourceSub'] == source_sub and
               'Timestamp' in row and
               datetime.strptime(row['Timestamp'], '%Y-%m-%d %H:%M:%S') > now - timedelta(days=1))

def already_listed(user):
    rows = sheet.col_values(1)
    return user.lower() in (u.lower() for u in rows)

def already_logged_action(log_id):
    ids = sheet.col_values(6)  # Column F = ModLogID
    return log_id in ids

def is_forgiven(user):
    records = sheet.get_all_records()
    for row in records:
        if row['Username'].lower() == user.lower():
            if str(row.get('ManualOverride', '')).strip().lower() in {'yes', 'true'}:
                return True
    return False

# --- Sync bans from modlogs into sheet ---

def sync_bans_from_sub(sub_name):
    subreddit = reddit.subreddit(sub_name)
    for log in subreddit.mod.log(action='banuser', limit=50):
        user = log.target_author
        reason = log.description or ""
        moderator = getattr(log, "mod", "unknown")
        source_sub = f"r/{log.subreddit}"
        log_id = log.id
        timestamp = datetime.utcfromtimestamp(log.created_utc).strftime('%Y-%m-%d %H:%M:%S')

        if already_logged_action(log_id):
            print(f"[SKIP] Already processed modlog ID {log_id}")
            continue

        if already_listed(user):
            print(f"[SKIP] User {user} already listed — skipping duplicate log")
            continue

        print(f"[MODLOG] {user} from {source_sub} — reason: {reason} — mod: {moderator}")

        if reason.strip().lower() != CROSS_SUB_BAN_REASON.lower():
            continue
        if source_sub not in TRUSTED_SOURCES:
            continue
        if user in EXEMPT_USERS or is_mod(subreddit, user):
            continue
        if get_recent_sheet_entries(source_sub) >= DAILY_BAN_LIMIT:
            print(f"[SKIP] {source_sub} hit daily limit for {user}")
            continue

        sheet.append_row([user, source_sub, reason, timestamp, "", log_id])
        print(f"[LOGGED] {user} from {source_sub} — modlog ID: {log_id}")

# --- Enforce bans locally based on sheet entries ---

def enforce_bans_on_sub(sub_name):
    subreddit = reddit.subreddit(sub_name)
    current_bans = {ban.name.lower() for ban in subreddit.banned(limit=None)}
    rows = sheet.get_all_records()

    for row in rows:
        user = row['Username']
        source_sub = row['SourceSub']
        reason = row['Reason']

        if reason.strip().lower() != CROSS_SUB_BAN_REASON.lower():
            continue
        if source_sub not in TRUSTED_SOURCES:
            continue
        if is_forgiven(user):
            print(f"[SKIP] {user} is globally forgiven (ManualOverride = yes)")
            continue
        if user.lower() in current_bans:
            continue
        if user.lower() in EXEMPT_USERS:
            continue
        if is_mod(subreddit, user):
            continue

        subreddit.banned.add(user, reason=f"Cross-sub ban from {source_sub} – {reason}")
        print(f"[BANNED] {user} in {sub_name}")

# --- Main execution ---

if __name__ == "__main__":
    for sub in TRUSTED_SUBS:
        print(f"--- Checking modlog for {sub}")
        sync_bans_from_sub(sub)

    for sub in TRUSTED_SUBS:
        print(f"--- Enforcing bans in {sub}")
        enforce_bans_on_sub(sub)
