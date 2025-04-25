import json
import base64
import os
import praw
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

# --- Load config from JSON file ---
with open("config.json") as config_file:
    config = json.load(config_file)

CROSS_SUB_BAN_REASON = config["CROSS_SUB_BAN_REASON"]
EXEMPT_USERS = set(config["EXEMPT_USERS"])
DAILY_BAN_LIMIT = config["DAILY_BAN_LIMIT"]
MAX_LOG_AGE_MINUTES = config["MAX_LOG_AGE_MINUTES"]
PUBLIC_LOG_PATH = "public_ban_log.json"
PUBLIC_LOG_MARKDOWN = "public_ban_log.md"

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

# --- Ban/unban public log ---
def log_public_action(action, username, sub_name, source_sub="", mod_name="", note=""):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    entry = {
        "timestamp": now,
        "action": action,
        "username": username,
        "subreddit": sub_name,
        "source_sub": source_sub,
        "mod": mod_name,
        "note": note
    }
    try:
        if os.path.exists(PUBLIC_LOG_PATH):
            with open(PUBLIC_LOG_PATH, 'r') as f:
                data = json.load(f)
        else:
            data = []
        data.append(entry)
        with open(PUBLIC_LOG_PATH, 'w') as f:
            json.dump(data, f, indent=2)

        with open(PUBLIC_LOG_MARKDOWN, 'a') as f:
            f.write(f"### [{now}] {'✅' if action == 'UNBANNED' else '❌'} {action} u/{username}\n")
            f.write(f"- **Subreddit**: r/{sub_name}\n")
            if source_sub:
                f.write(f"- **Source Sub**: {source_sub}\n")
            f.write(f"- **Moderator**: {mod_name}\n")
            if note:
                f.write(f"- **Note**: {note}\n")
            f.write(f"---\n\n")
    except Exception as e:
        print(f"[ERROR] Failed to log public action: {e}")

# --- Enforce bans locally based on sheet entries ---
def enforce_bans_on_sub(sub_name):
    try:
        subreddit = reddit.subreddit(sub_name)
        current_bans = {ban.name.lower(): ban for ban in subreddit.banned(limit=None)}
    except Exception as e:
        print(f"[ERROR] Could not get ban list for {sub_name}: {e}")
        return

    rows = sheet.get_all_records()

    for row in rows:
        user = row['Username']
        source_sub = row['SourceSub']
        user_lower = user.lower()
        already_banned = user_lower in current_bans
        is_mod_user = is_mod(subreddit, user)
        is_exempt = user_lower in EXEMPT_USERS
        is_override = is_forgiven(user)

        if is_override:
            if already_banned:
                try:
                    ban_obj = current_bans[user_lower]
                    ban_reason_text = getattr(ban_obj, "note", "") or ""
                    if CROSS_SUB_BAN_REASON.lower() in ban_reason_text.lower():
                        subreddit.banned.remove(user)
                        print(f"[UNBANNED] {user} in {sub_name} (forgiven and ban matched reason)")
                        log_public_action("UNBANNED", user, sub_name, source_sub, "auto", "Forgiven")
                except Exception as e:
                    print(f"[ERROR] Failed to unban {user} in {sub_name}: {e}")
            continue

        if not already_banned:
            if str(row.get('ManualOverride', '')).strip().lower() not in {'yes', 'true'}:
                print(f"[NOTICE] {user} was manually unbanned in {sub_name} without override")
                try:
                    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                    sheet.append_row([user, source_sub, "Manual unban detected", now, "", "", "", sub_name, now])
                except Exception as e:
                    print(f"[ERROR] Failed to log manual unban for {user}: {e}")
            continue

        if already_banned or is_exempt or is_mod_user:
            continue

        try:
            subreddit.banned.add(
                user,
                ban_reason=CROSS_SUB_BAN_REASON,
                note=f"Cross-sub ban from {source_sub}"
            )
            print(f"[BANNED] {user} in {sub_name}")
            log_public_action("BANNED", user, sub_name, source_sub, "auto", "")
        except Exception as e:
            print(f"[ERROR] Failed to ban {user} in {sub_name}: {e}")

# --- Main execution ---
if __name__ == "__main__":
    check_modmail_for_overrides()

    for sub in TRUSTED_SUBS:
        print(f"--- Checking modlog for {sub}")
        sync_bans_from_sub(sub)

    for sub in TRUSTED_SUBS:
        print(f"--- Enforcing bans in {sub}")
        enforce_bans_on_sub(sub)
