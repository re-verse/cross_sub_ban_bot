import time
import traceback
import prawcore
import sys
from datetime import datetime, timedelta

# Assuming these are imported from a config file
# This is just for context based on the code provided
from bot_config import (
    CROSS_SUB_BAN_REASON,
    EXEMPT_USERS,
    DAILY_BAN_LIMIT,
    MAX_LOG_AGE_MINUTES,
    ROW_RETENTION_DAYS,
    TRUSTED_SUBS,
    TRUSTED_SOURCES,
    database,  # This is our DatabaseWrapper
    reddit,
    USE_SQLITE,
    SQLITE_DB_PATH
)

# --- Caches ---
mod_cache = {}
SHEET_CACHE = []

# --- Helper Functions ---

def load_sheet_cache():
    """
    Load data into cache - now using SQLite for massive performance gain
    """
    global SHEET_CACHE
    try:
        start = time.time()
        SHEET_CACHE = database.get_all_records()
        load_time = time.time() - start
        print(f"[DEBUG] Database load took {load_time:.2f}s")
        print(f"[INFO] Loaded {len(SHEET_CACHE)} rows into local cache.")
        
        # Show performance improvement message
        if USE_SQLITE:
            print(f"[PERFORMANCE] SQLite load: {load_time:.2f}s (vs ~2-5s with Google Sheets)")
        
    except Exception as e:
        print(f"[ERROR] Failed to load database cache: {e}")
        SHEET_CACHE = []

def get_recent_sheet_entries_optimized(source_sub):
    """
    Get recent entries count - optimized for SQLite
    """
    if USE_SQLITE:
        # Use direct database query for better performance
        return database.db.get_recent_entries(source_sub, hours=24)
    else:
        # Fall back to cache scanning for Google Sheets
        from core_utils import get_recent_sheet_entries
        return get_recent_sheet_entries(source_sub, SHEET_CACHE)

# --- Ban Sync ---
def sync_bans_from_sub(sub):
    """
    Main ban synchronization function - optimized for SQLite performance
    """
    print(f"[STEP] Checking modlog for r/{sub}")
    seen_user_sources = set()

    try:
        sr = reddit.subreddit(sub)

        print(f"[INFO] Scanning latest 200 mod actions for r/{sub}...")
        for log in sr.mod.log(limit=200):  # includes both ban and unban actions
            log_id = log.id
            mod = getattr(log.mod, 'name', 'unknown')
            action = log.action
            desc = (log.description or '').strip()
            source = f"r/{log.subreddit}".lower()
            ts = datetime.utcfromtimestamp(log.created_utc)
            user = getattr(log, "target_author", None)

            if not isinstance(user, str) or not user.strip():
                user = "[unknown_user]"

            if user == "[unknown_user]":
                if action in ("banuser", "unbanuser"):
                    print(f"[WARN] Skipping log {log_id} - No valid target user found")
                continue

            if datetime.utcnow() - ts > timedelta(minutes=MAX_LOG_AGE_MINUTES):
                continue

            user_lc = user.strip().lower()

            # --- Handle UNBAN actions ---
            if action == "unbanuser":
                handle_unban_action(user, user_lc, source, mod, sub, ts)
                continue

            # --- Handle BAN actions ---
            if action == "banuser":
                handle_ban_action(user, user_lc, source, mod, sub, ts, log_id, seen_user_sources)

    except prawcore.exceptions.Forbidden as e:
        print(f"[ERROR] Access forbidden for r/{sub}: {e}")
    except Exception as e:
        print(f"[ERROR] Failed to process r/{sub}: {e}")
        traceback.print_exc()

def handle_unban_action(user, user_lc, source, mod, sub, ts):
    """
    Handle unban actions - optimized for SQLite
    """
    print(f"[UNBAN] u/{user} unbanned in {source} by {mod}")
    
    # Check if this user was in our ban list from this source
    user_records = [row for row in SHEET_CACHE
                    if row.get('Username', '').lower() == user_lc
                    and row.get('SourceSub', '') == source]
    
    if user_records:
        forgive_time = ts.strftime('%Y-%m-%d %H:%M:%S')
        
        if USE_SQLITE:
            # Use optimized SQLite update
            success = database.update_forgiveness(user, source, mod, sub, forgive_time)
            if success:
                print(f"[FORGIVE] u/{user} unbanned in {source} by {mod} – marked as forgiven.")
                # Update local cache
                for row in user_records:
                    row["ManualOverride"] = "yes"
                    row["OverriddenBy"] = mod
                    row["ModSub"] = sub
                    row["ForgiveTimestamp"] = forgive_time
            else:
                print(f"[ERROR] Failed to update forgiveness for u/{user}")
        else:
            # Fall back to Google Sheets method (slower)
            handle_unban_google_sheets(user, user_lc, source, mod, sub, forgive_time)

def handle_unban_google_sheets(user, user_lc, source, mod, sub, forgive_time):
    """
    Handle unban for Google Sheets backend (legacy method)
    """
    # Find the row in Google Sheets and update
    for i, row in enumerate(SHEET_CACHE, start=2):  # start=2 because sheet rows are 1-indexed + header
        if (row.get('Username', '').lower() == user_lc and
            row.get('SourceSub', '') == source and
            not row.get('ManualOverride', '')):
            
            try:
                from bot_config import sheet  # Import original sheet for Google Sheets updates
                sheet.update_cell(i, 5, "yes")  # ManualOverride
                sheet.update_cell(i, 7, mod)    # OverriddenBy
                sheet.update_cell(i, 8, sub)    # ModSub
                sheet.update_cell(i, 9, forgive_time)  # ForgiveTimestamp
                
                # Update local cache
                SHEET_CACHE[i-2]["ManualOverride"] = "yes"
                SHEET_CACHE[i-2]["OverriddenBy"] = mod
                SHEET_CACHE[i-2]["ModSub"] = sub
                SHEET_CACHE[i-2]["ForgiveTimestamp"] = forgive_time
                
                print(f"[FORGIVE] u/{user} unbanned in {source} by {mod} – marked as forgiven.")
                break
            except Exception as e:
                print(f"[ERROR] Failed to update forgiveness for u/{user}: {e}")

def handle_ban_action(user, user_lc, source, mod, sub, ts, log_id, seen_user_sources):
    """
    Handle ban actions - optimized for SQLite
    """
    # Skip if user is exempt
    if user_lc in EXEMPT_USERS:
        print(f"[SKIP] User {user} is in EXEMPT_USERS list")
        return

    # Skip if user is forgiven
    if is_forgiven(user, SHEET_CACHE): # Assuming is_forgiven is defined elsewhere
        print(f"[SKIP] User {user} has ManualOverride=yes")
        return

    # Rate limiting check - optimized for SQLite
    if USE_SQLITE:
        recent_count = database.db.get_recent_entries(source, hours=24)
    else:
        from core_utils import get_recent_sheet_entries
        recent_count = get_recent_sheet_entries(source, SHEET_CACHE)
    
    if recent_count >= DAILY_BAN_LIMIT:
        print(f"[SKIP] Daily limit ({DAILY_BAN_LIMIT}) reached for {source}")
        return

    # Check if already processed
    if user_lc in seen_user_sources:
        print(f"[SKIP] Already logged user {user_lc} to sheet (from any sub)")
        return
    seen_user_sources.add(user_lc)

    # Add to database
    try:
        row_data = [
            user,
            source,
            CROSS_SUB_BAN_REASON,
            ts.strftime('%Y-%m-%d %H:%M:%S'),
            '',  # ManualOverride
            log_id,
            mod,
            '',  # ModSub
            '',  # ForgiveTimestamp
            ''   # ExemptSubs
        ]
        
        print("[DEBUG] About to add row:", row_data)
        
        # Use database wrapper for consistent interface
        success = database.append_row(row_data)
        
        if success:
            print(f"[SUCCESS] Logged u/{user} ban from {source}")
            
            # Update local cache
            SHEET_CACHE.append({
                'Username': user,
                'SourceSub': source,
                'Reason': CROSS_SUB_BAN_REASON,
                'Timestamp': ts.strftime('%Y-%m-%d %H:%M:%S'),
                'ManualOverride': '',
                'LogID': log_id,
                'ModeratorName': mod,
                'ModSub': '',
                'ForgiveTimestamp': '',
                'ExemptSubs': ''
            })
        else:
            print(f"[ERROR] Failed to log u/{user} ban from {source}")
            
    except Exception as e:
        print(f"[ERROR] FAILED to log user '{user}' for r/{sub}: {e}")
        traceback.print_exc()

def execute_cross_sub_bans():
    """
    Execute cross-subreddit bans - optimized for SQLite performance
    """
    print("[STEP] Executing cross-sub bans...")
    
    ban_count = 0
    processed_users = set()
    
    for record in SHEET_CACHE:
        username = record.get('Username', '').strip()
        source_sub = record.get('SourceSub', '')
        
        if not username or not source_sub:
            continue
            
        # Skip if already processed or forgiven
        if username.lower() in processed_users:
            continue
        if is_forgiven(username, SHEET_CACHE): # Assuming is_forgiven is defined elsewhere
            continue
            
        processed_users.add(username.lower())
        
        # Get user's exempt subreddits
        user_exempt_subs = exempt_subs_for_user(username, SHEET_CACHE) # Assuming this is defined
        
        # Ban in trusted subreddits (except exempt ones)
        for target_sub in TRUSTED_SUBS:
            if target_sub in user_exempt_subs:
                print(f"[SKIP] u/{username} exempt from r/{target_sub}")
                continue
                
            try:
                execute_ban_in_subreddit(username, target_sub, source_sub)
                ban_count += 1
                
                # Rate limiting
                time.sleep(1)  # Prevent rate limiting
                
            except Exception as e:
                print(f"[ERROR] Failed to ban u/{username} in r/{target_sub}: {e}")
    
    print(f"[STEP] Cross-sub ban execution completed. Total bans: {ban_count}")

def execute_ban_in_subreddit(username, target_sub, source_sub):
    """
    Execute a ban in a specific subreddit
    """
    try:
        sr = reddit.subreddit(target_sub)
        
        # Check if user is already banned
        try:
            # PRAW 7+ returns a Redditor object if banned, raises exception if not.
            # This logic might need adjustment based on PRAW version.
            next(sr.banned(redditor=username))
            print(f"[SKIP] u/{username} already banned in r/{target_sub}")
            return
        except (StopIteration, prawcore.exceptions.NotFound):
            pass  # User not banned, proceed
        
        # Execute the ban
        ban_reason = f"{CROSS_SUB_BAN_REASON} (source: {source_sub})"
        sr.banned.add(username, ban_reason=ban_reason)
        
        print(f"[BAN] u/{username} banned in r/{target_sub} (source: {source_sub})")
        
        # Log the action
        log_public_action(username, target_sub, source_sub, ban_reason) # Assuming this is defined
        
    except prawcore.exceptions.Forbidden:
        print(f"[ERROR] No permission to ban in r/{target_sub}")
    except Exception as e:
        print(f"[ERROR] Failed to ban u/{username} in r/{target_sub}: {e}")

def cleanup_old_records():
    """
    Clean up old records - optimized for SQLite
    """
    if USE_SQLITE:
        deleted_count = database.cleanup_old_records(ROW_RETENTION_DAYS)
        print(f"[CLEANUP] Removed {deleted_count} old records")
        
        # Refresh cache after cleanup
        load_sheet_cache()
    else:
        print("[CLEANUP] Old record cleanup not implemented for Google Sheets backend")

def show_performance_stats():
    """
    Show database performance statistics
    """
    if USE_SQLITE:
        stats = database.get_stats()
        print(f"[STATS] Database Statistics:")
        print(f"  Total records: {stats['total_records']}")
        print(f"  Active bans: {stats['active_bans']}")
        print(f"  Forgiven bans: {stats['forgiven_bans']}")
        print(f"  Recent activity (24h): {stats['recent_24h']}")
        print(f"  Database backend: SQLite ({SQLITE_DB_PATH})")
    else:
        print(f"[STATS] Using Google Sheets backend - {len(SHEET_CACHE)} records loaded")

# --- Main execution ---
def main():
    """
    Main bot execution function
    """
    print("="*60)
    print("Cross-Sub Ban Bot - SQLite Edition")
    print(f"Database backend: {'SQLite' if USE_SQLITE else 'Google Sheets'}")
    print("="*60)
    
    try:
        # Load data cache
        print("[STEP] Loading database cache...")
        load_sheet_cache()
        
        # Show performance stats
        show_performance_stats()
        
        # Check for superuser commands
        if len(sys.argv) > 1:
            # Assuming check_superuser_command is defined elsewhere
            result = check_superuser_command(sys.argv[1:])
            if result:
                return
        
        # Check modmail for overrides
        print("[STEP] Checking modmail...")
        check_modmail() # Assuming check_modmail is defined elsewhere
        
        # Sync bans from trusted subreddits
        print("[STEP] Syncing bans from trusted subreddits...")
        for sub in TRUSTED_SUBS:
            sync_bans_from_sub(sub)
            time.sleep(2)  # Rate limiting
        
        # Execute cross-sub bans
        execute_cross_sub_bans()
        
        # Clean up old records (SQLite only)
        if USE_SQLITE:
            cleanup_old_records()
        
        # Write statistics
        print("[STEP] Writing statistics...")
        write_stats_sheet() # Assuming write_stats_sheet is defined elsewhere
        
        # Flush public logs
        flush_public_markdown_log() # Assuming this is defined
        
        print("[SUCCESS] Bot execution completed successfully!")
        
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Bot execution cancelled by user")
    except Exception as e:
        print(f"[ERROR] Bot execution failed: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
