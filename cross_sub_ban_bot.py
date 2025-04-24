import praw
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

# --- CONFIGURATION ---

# Your subreddit name (no "r/")
SUBREDDIT_NAME = "your_trial_subreddit"

# Your trusted sources
TRUSTED_SOURCES = {'r/leafs', 'r/ottawasenators'}  # Add others as needed

# Mods or bot users that should never be banned
EXEMPT_USERS = {'AutoModerator', 'YourModAccount'}

# Daily limit per source subreddit
DAILY_BAN_LIMIT = 10

# Google Sheet ID (from URL)
SHEET_ID = "your_google_sheet_id_here"

# Path to your service account JSON
SERVICE_ACCOUNT_FILE = "path/to/your_service_account.json"

# --- SETUP REDDIT API (PRAW) ---

reddit = praw.Reddit(
    client_id="your_client_id",
    client_secret="your_client_secret",
    username="your_bot_username",
    password="your_bot_password",
    user_agent="NHL Cross-Sub Ban Bot v1.0"
)

# --- SETUP GOOGLE SHEETS API ---

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID).sheet1

# --- UTILS ---

def get_recent_sheet_entries(source_sub):
    """Count how many bans a sub has added in the last 24h"""
    rows = sheet.get_all_records()
    now = datetime.utcnow()
    return sum(1 for row in rows
               if row['SourceSub'] == source_sub and
               'Timestamp' in row and
               datetime.strptime(row['Timestamp'], '%Y-%m-%d %H:%M:%S') > now - timedelta(days=1))

def already_listed(user):
    """Check if user already exists in the sheet"""
    rows = sheet.col_values(1)  # Username column
    return user.lower() in (u.lower() for u in rows)

def is_mod(user):
    """Check if the user is a current mod of this sub"""
    mods = [mod.name.lower() for mod in reddit.subreddit(SUBREDDIT_NAME).moderator()]
    return user.lower() in mods

# --- MAIN BAN LOGIC ---

def sync_bans():
    subreddit = reddit.subreddit(SUBREDDIT_NAME)
    modlog = subreddit.mod.log(action='banuser', limit=50)

    for log in modlog:
        user = log.target_author
        reason = log.details or ''
        source_sub = f"r/{log.subreddit.display_name}"
        timestamp = datetime.utcfromtimestamp(log.created_utc).strftime('%Y-%m-%d %H:%M:%S')

        # Only process Cross-sub trolling bans
        if reason.strip().lower() != "cross-sub trolling":
            continue

        if source_sub not in TRUSTED_SOURCES:
            continue

        if user in EXEMPT_USERS or is_mod(user) or already_listed(user):
            continue

        if get_recent_sheet_entries(source_sub) >= DAILY_BAN_LIMIT:
            print(f"[SKIP] {source_sub} hit daily limit for {user}")
            continue

        sheet.append_row([user, source_sub, reason, timestamp, ""])
        print(f"[LOGGED] Added {user} for cross-sub trolling")

def enforce_sheet_bans():
    subreddit = reddit.subreddit(SUBREDDIT_NAME)
    current_bans = {ban.user.name.lower() for ban in subreddit.banned(limit=None)}
    rows = sheet.get_all_records()

    for row in rows:
        user = row['Username']
        source_sub = row['SourceSub']
        reason = row['Reason']
        override = str(row.get('ManualOverride', '')).strip().lower()

        if reason.strip().lower() != "cross-sub trolling":
            continue

        if source_sub not in TRUSTED_SOURCES:
            continue

        if override in {'true', 'yes'}:
            continue

        if user.lower() not in current_bans and user.lower() not in EXEMPT_USERS and not is_mod(user):
            subreddit.banned.add(user, reason=f"Cross-sub ban from {source_sub} – {reason}")
            print(f"[BANNED] {user} from {SUBREDDIT_NAME}")

# --- RUN ---

if __name__ == "__main__":
    sync_bans()
    enforce_sheet_bans()
