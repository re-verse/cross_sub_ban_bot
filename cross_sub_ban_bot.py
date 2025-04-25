import json
import base64
import os
import praw
import gspread
import prawcore
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

def is_mod(subreddit, username):
    try:
        if subreddit.display_name.lower() not in mod_cache:  # Make sure display_name is lowercased
            mod_cache[subreddit.display_name.lower()] = {mod.name.lower() for mod in subreddit.moderator()}
        return username.lower() in mod_cache[subreddit.display_name.lower()]
    except prawcore.exceptions.NotFound:
        print(f"[WARN] Subreddit r/{subreddit.display_name} not found while checking mod status.")
        return False
    except Exception as e:
        print(f"[ERROR] Error checking mod status for r/{subreddit.display_name}: {e}")
        return False


def is_forgiven(username):
    try:
        rows = sheet.get_all_records()
        for row in rows:
            if row['Username'].lower() == username.lower() and str(row.get("ManualOverride", "")).lower() in {"yes", "true"}:
                return True
    except Exception as e:
        print(f"[ERROR] Error checking if user is forgiven: {e}")
        return False  # Important: Return False on error to avoid unintended behavior
    return False



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

# --- Sync bans from modlog ---
def sync_bans_from_sub(sub):
    subreddit = reddit.subreddit(sub)
    print(f"--- Checking modlog for {sub}")
    try:
        for log in subreddit.mod.log(action='banuser', limit=50):
            if log.created_utc < (datetime.utcnow() - timedelta(minutes=MAX_LOG_AGE_MINUTES)).timestamp():
                print(f"[SKIP] Modlog too old for {log.target_author}, ignoring ID {log.id}")
                continue
            if not log.target_author:
                continue
            reason = (log.details or "").lower()
            if CROSS_SUB_BAN_REASON.lower() not in reason:
                continue
            already_banned = already_listed(log.target_author) # changed from using sheet.get_all_values()
            user_lower = log.target_author.lower()
            if already_banned:
                continue
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            sheet.append_row([
                log.target_author, sub, log.details, now, "", log.id, "", "", now
            ])
            print(f"[LOGGED] {log.target_author} from r/{sub} — modlog ID: {log.id}")
    except prawcore.exceptions.Forbidden:
        print(f"[WARN] Bot does not have access to r/{sub} — likely not a mod yet. Skipping.")
    except prawcore.exceptions.NotFound:
        print(f"[WARN] Subreddit r/{sub} not found. Possibly private or banned. Skipping.")
    except Exception as e:
        print(f"[ERROR] Unexpected error for r/{sub}: {e}")

# --- Enforce bans locally based on sheet entries ---
def enforce_bans_on_sub(sub_name):
    subreddit = reddit.subreddit(sub_name)
    try:
        current_bans = {ban.name.lower(): ban for ban in subreddit.banned(limit=None)}
    except prawcore.exceptions.Forbidden:
        print(f"[WARN] Bot does not have access to r/{sub_name} — likely not a mod yet. Skipping.")
        return
    except prawcore.exceptions.NotFound:
        print(f"[WARN] Subreddit r/{sub_name} not found. Possibly private or banned. Skipping.")
        return
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
                    else:
                        print(f"[SKIP] {user} is forgiven, but existing ban doesn't match bot reason")
                except Exception as e:
                    print(f"[ERROR] Failed to unban {user} in {sub_name}: {e}")
            else:
                print(f"[SKIP] {user} is globally forgiven and not banned in {sub_name}")
            continue

        if already_banned or is_exempt or is_mod_user:
            continue

        # Check for manual unbans *before* attempting to ban
        if not already_banned and is_override:
            continue

        if already_banned and not is_override:
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

# --- Check modmail for overrides (placeholder) ---
def check_modmail_for_overrides():
    print("[DEBUG] Starting modmail override check...")
    # You'd implement the actual override logic here
    try:
        for sub in TRUSTED_SUBS:
            subreddit = reddit.subreddit(sub)
            for state in ["new", "mod", "all"]:
                for convo in subreddit.modmail.conversations(state=state):
                    if not convo.messages:
                        print(f"[DEBUG] Skipping empty conversation ID {convo.id}")
                        continue

                    last_message = convo.messages[-1]
                    body = last_message.body_markdown.strip() if last_message.body_markdown else ""
                    author = last_message.author

                    if not author:
                        print(f"[DEBUG] Skipping message with no author in convo ID {convo.id}")
                        continue

                    sender = author.name.lower()
                    print(f"[DEBUG] Modmail from {sender}: {body}")

                    if not is_trusted_mod(sender):
                        print(f"[DENIED] Modmail from non-mod user: {sender}")
                        continue

                    if body.lower().startswith("/xsub pardon"):
                        parts = body.strip().split();
                        if len(parts) >= 3:
                            username = parts[2].lstrip("u/").strip()
                            if apply_override(username, sender, sub.display_name):
                                convo.reply(f"✅ u/{username} has been marked as forgiven. They will not be banned again.")
                                print(f"[OVERRIDE] {username} set by {sender} in {sub.display_name}")
                            else:
                                convo.reply(f"⚠️ u/{username} was not found in the sheet. No action taken.")
                                print(f"[NOT FOUND] {username} from modmail")
    except Exception as e:
        print(f"[ERROR] Modmail check failed: {e}")

# --- Main execution ---
if __name__ == "__main__":
    check_modmail_for_overrides()
    for sub in TRUSTED_SUBS:
        sync_bans_from_sub(sub)

    for sub in TRUSTED_SUBS:
        enforce_bans_on_sub(sub)
