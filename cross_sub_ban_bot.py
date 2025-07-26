import time
import traceback
import prawcore
import sys
from datetime import datetime, timedelta

from bot_config import (
    CROSS_SUB_BAN_REASON,
    EXEMPT_USERS,
    DAILY_BAN_LIMIT,
    MAX_LOG_AGE_MINUTES,
    ROW_RETENTION_DAYS,
    TRUSTED_SUBS,
    database,
    reddit
)

# --- Global Cache ---
BAN_CACHE = []

# --- Helper Functions ---
def is_forgiven(username, cache):
    """Check if a user has been manually forgiven."""
    for record in cache:
        if record.get('username', '').lower() == username.lower():
            if record.get('manual_override', '').lower() == 'yes':
                return True
    return False

# --- Core Bot Logic ---
def load_ban_cache():
    """Load all ban records from the database into the local cache."""
    global BAN_CACHE
    try:
        start_time = time.time()
        BAN_CACHE = database.get_all_records()
        load_time = time.time() - start_time
        print(f"[INFO] Loaded {len(BAN_CACHE)} records from DB in {load_time:.2f}s.")
    except Exception as e:
        print(f"[ERROR] Failed to load database cache: {e}")
        BAN_CACHE = []

def sync_bans_from_sub(sub):
    """Fetch recent ban/unban actions from a subreddit's modlog."""
    print(f"[SYNC] Checking modlog for r/{sub}...")
    try:
        subreddit = reddit.subreddit(sub)
        for log in subreddit.mod.log(limit=100):
            if datetime.utcnow() - datetime.utcfromtimestamp(log.created_utc) > timedelta(minutes=MAX_LOG_AGE_MINUTES):
                break # Modlog is chronological, so we can stop early

            user = getattr(log, "target_author", None)
            if not user or ' ' in user: # Skip if no user or invalid username
                continue

            user_lc = user.lower()
            source = f"r/{log.subreddit}".lower()

            if log.action == "banuser":
                handle_ban_action(user, user_lc, source, log.mod.name, sub, log.created_utc, log.id)
            elif log.action == "unbanuser":
                handle_unban_action(user, user_lc, source, log.mod.name, sub, log.created_utc)

    except prawcore.exceptions.Forbidden as e:
        print(f"[ERROR] Access forbidden for r/{sub}: {e}")
    except Exception as e:
        print(f"[ERROR] Failed to process r/{sub}: {e}")
        traceback.print_exc()

def handle_unban_action(user, user_lc, source, mod, sub, timestamp):
    """Process an unban action by marking the user as forgiven in the database."""
    print(f"[UNBAN] Detected unban for u/{user} in {source} by {mod}.")
    forgiven_time = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
    success = database.update_forgiveness(user, source, mod, sub, forgiven_time)
    if success:
        print(f"[FORGIVE] Marked u/{user} as forgiven in the database.")
        # Update local cache to reflect the change immediately
        for record in BAN_CACHE:
            if record.get('username', '').lower() == user_lc and record.get('source_sub') == source:
                record['manual_override'] = 'yes'
                break

def handle_ban_action(user, user_lc, source, mod, sub, timestamp, log_id):
    """Process a ban action by logging it to the database."""
    if user_lc in EXEMPT_USERS:
        print(f"[SKIP] User u/{user} is exempt.")
        return

    if is_forgiven(user, BAN_CACHE):
        print(f"[SKIP] User u/{user} has a manual override (forgiven).")
        return

    # Prevent re-adding a ban that's already in our database
    if any(r.get('username', '').lower() == user_lc and r.get('source_sub') == source for r in BAN_CACHE):
        # print(f"[DEBUG] Ban for u/{user} from {source} already in database.")
        return

    recent_count = database.get_recent_entries(source, hours=24)
    if recent_count >= DAILY_BAN_LIMIT:
        print(f"[SKIP] Daily limit ({DAILY_BAN_LIMIT}) reached for {source}.")
        return

    print(f"[BAN] Detected ban for u/{user} in {source}. Logging to DB.")
    row_data = [
        user,
        source,
        CROSS_SUB_BAN_REASON,
        datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S'),
        '', # manual_override
        log_id,
        mod
    ]
    if database.append_row(row_data):
        # Add to local cache to avoid re-processing in the same run
        BAN_CACHE.append({'username': user, 'source_sub': source, 'manual_override': 'no'})

def main():
    """Main bot execution function."""
    print("="*60)
    print("Cross-Sub Ban Bot - SQLite Edition")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    try:
        print("\n[PHASE 1] Loading Data")
        load_ban_cache()

        print("\n[PHASE 2] Syncing Bans from Source Subreddits")
        for sub in TRUSTED_SUBS:
            sync_bans_from_sub(sub)

        print("\n[PHASE 3] Database Maintenance")
        deleted_count = database.cleanup_old_records(ROW_RETENTION_DAYS)
        if deleted_count > 0:
            print(f"[CLEANUP] Removed {deleted_count} records older than {ROW_RETENTION_DAYS} days.")

        print("\n[SUCCESS] Bot execution completed successfully!")

    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Bot execution cancelled by user.")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Bot execution failed: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
