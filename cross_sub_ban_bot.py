import praw
import gspread
import os
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import base64
import json

# --- CONFIGURATION ---
SUBREDDIT_NAME = "xsubpacttest1"  # The sub this bot is running for
CROSS_SUB_BAN_REASON = "Auto XSub Pact Ban"
EXEMPT_USERS = {"AutoModerator", "xsub-pact-bot"}
DAILY_BAN_LIMIT = 10

# --- TRUSTED SOURCE LOADER ---

def load_trusted_subs(file_path="trusted_subs.txt"):
    with open(file_path, "r") as f:
        return {"r/" + line.strip() for line in f if line.strip()}

TRUSTED_SOURCES = load_trusted_subs()

# --- GOOGLE SHEETS AUTH ---

creds_json = base64.b64decode(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
creds_dict = json.loads(creds_json)

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.environ['GOOGLE_SHEET_ID']).sheet1

# --- REDDIT AUTH ---

reddit = praw.Reddit(
    client_id=os.environ['CLIENT_ID'],
    client_secret=os.environ['CLIENT_SECRET'],
    username=os.environ['USERNAME'],
    password=os.environ['PASSWORD'],
    user_agent='NHL Cross-Sub Ban Bot'
)

# --- UTILS ---

def get_recent_sheet_entries(source_sub):
    now = datetime.utcnow()
    return sum(1 for row in sheet.get_all_records()
               if row['SourceSub'] == source_sub and
               'Timestamp' in row and
               datetime.strptime(row['Timestamp'], '%Y-%m-%d %H:%M:%S') > now - timedelta(days=1))

def already_listed(user):
    rows = sheet.col_values(1)
    return user.lower() in (u.lower() for u in rows)

def is_mod(user):
    mods = [mod.name.lower() for mod in reddit.subreddit(SUBREDDIT_NAME).moderator()]
    return user.lower() in mods

# --- SYNC NEW BANS TO SHEET ---

def sync_bans():
    subreddit = reddit.subreddit(SUBREDDIT_NAME)
    modlog = subreddit.mod.log(action='banuser', limit=50)

    for log in modlog:
        user = log.target_author
        reason = log.details or ''
        source_sub = f"r/{log.subreddit}"

        timestamp = datetime.utcfromtimestamp(log.created_utc).strftime('%Y-%m-%d %H:%M:%S')

        if reason.strip().lower() != CROSS_SUB_BAN_REASON.lower():
            continue
        if source_sub not in TRUSTED_SOURCES:
            continue
        if user in EXEMPT_USERS or is_mod(user) or already_listed(user):
            continue
        if get_recent_sheet_entries(source_sub) >= DAILY_BAN_LIMIT:
            print(f"[SKIP] {source_sub} hit daily limit for {user}")
            continue

        sheet.append_row([user, source_sub, reason, timestamp, ""])
        print(f"[LOGGED] Added {user} for {CROSS_SUB_BAN_REASON}")

# --- ENFORCE SHEET BANS LOCALLY ---

def enforce_sheet_bans():
    subreddit = reddit.subreddit(SUBREDDIT_NAME)
    current_bans = {ban.user.name.lower() for ban in subreddit.banned(limit=None)}
    rows = sheet.get_all_records()

    for row in rows:
        user = row['Username']
        source_sub = row['SourceSub']
        reason = row['Reason']
        override = str(row.get('ManualOverride', '')).strip().lower()

        if reason.strip().lower() != CROSS_SUB_BAN_REASON.lower():
            continue
        if source_sub not in TRUSTED_SOURCES:
            continue
        if override in {'true', 'yes'}:
            continue
        if user.lower() not in current_bans and user.lower() not in EXEMPT_USERS and not is_mod(user):
            subreddit.banned.add(user, reason=f"Cross-sub ban from {source_sub} – {reason}")
            print(f"[BANNED] {user} from {SUBREDDIT_NAME}")

# --- MAIN ---

if __name__ == "__main__":
    sync_bans()
    enforce_sheet_bans()
