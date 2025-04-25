#!/usr/bin/env python3

import praw
import prawcore
import gspread
# You might use google.oauth2.service_account or oauth2client depending on your gspread setup
# from google.oauth2.service_account import Credentials # Example import
import os
import sys
import time
import json # If loading JSON string from env var
from datetime import datetime

# --- Configuration Constants (Modify as needed) ---

# List of subreddits the bot should manage
TARGET_SUBREDDITS = ["YourSubreddit1", "YourSubreddit2"]

# Environment variables for credentials (Ensure these match your GitHub Secrets names!)
# Option 1: If using generic names (like in cross_sub_ban_boy.yml)
CLIENT_ID = os.environ.get('CLIENT_ID')
CLIENT_SECRET = os.environ.get('CLIENT_SECRET')
USERNAME = os.environ.get('USERNAME')
PASSWORD = os.environ.get('PASSWORD')
# Option 2: If using REDDIT_ prefixed names (like confirmed previously)
# CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID')
# CLIENT_SECRET = os.environ.get('REDDIT_CLIENT_SECRET')
# USERNAME = os.environ.get('REDDIT_USERNAME')
# PASSWORD = os.environ.get('REDDIT_PASSWORD')

GOOGLE_SHEET_ID_MAIN = os.environ.get('GOOGLE_SHEET_ID') # Sheet with master ban list
GOOGLE_SHEET_WORKSHEET_NAME_MAIN = "Sheet1" # Worksheet name in the main sheet
GOOGLE_SERVICE_ACCOUNT_JSON_STRING = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') # Full JSON key as string

# Public log configuration (if different sheet/worksheet) - **MODIFY IF NEEDED**
# GOOGLE_SHEET_ID_PUBLIC_LOG = os.environ.get('GOOGLE_SHEET_ID_PUBLIC') # Separate ID if needed
# GOOGLE_SHEET_WORKSHEET_NAME_PUBLIC_LOG = "PublicLog" # Worksheet name for public log

# Bot behavior constants
CROSS_SUB_BAN_REASON = "Violating cross-subreddit ban policy." # Default ban reason message for Reddit
EXEMPT_USERS = {"automoderator", "reddit", "your_main_mod_account"} # Set of lowercase usernames exempt from bot actions

# --- Authentication Functions ---

def authenticate_reddit():
    """Authenticates with Reddit using credentials from environment variables."""
    print("Attempting Reddit authentication...")
    if not all([CLIENT_ID, CLIENT_SECRET, USERNAME, PASSWORD]):
        print("[FATAL] Missing Reddit credentials in environment variables.")
        sys.exit(1)
    try:
        reddit = praw.Reddit(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            username=USERNAME,
            password=PASSWORD,
            user_agent=f"CrossSubBanBot v1.0 by /u/{USERNAME}", # Customize user agent
            ratelimit_seconds=10 # Increase default PRAW ratelimit handling
        )
        reddit.validate_on_submit = True # Good practice
        print(f"Successfully authenticated to Reddit as u/{reddit.user.me().name}")
        return reddit
    except prawcore.exceptions.OAuthException as e:
        print(f"[FATAL] Reddit OAuth Error (check credentials / app permissions): {type(e).__name__} - {e}")
        sys.exit(1)
    except prawcore.exceptions.ResponseException as e:
        print(f"[FATAL] Reddit Response Error during auth (Reddit might be down?): {type(e).__name__} - {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[FATAL] Unexpected error during Reddit authentication: {type(e).__name__} - {e}")
        sys.exit(1)

def authenticate_google():
    """Authenticates with Google Sheets using service account JSON from environment variable."""
    print("Attempting Google Sheets authentication...")
    if not GOOGLE_SERVICE_ACCOUNT_JSON_STRING:
        print("[FATAL] Missing GOOGLE_SERVICE_ACCOUNT_JSON environment variable.")
        sys.exit(1)
    try:
        # Load credentials from the JSON string
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON_STRING)
        # Define the required scopes for Google Sheets API
        scopes = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        # Create credentials object (adjust if using different library)
        # Example using google-auth library (install google-auth, google-auth-oauthlib, google-auth-httplib2)
        # from google.oauth2.service_account import Credentials
        # credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        # gc = gspread.authorize(credentials)

        # Example using older oauth2client with gspread (might still be common)
        # from oauth2client.service_account import ServiceAccountCredentials
        # credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scopes)
        # gc = gspread.authorize(credentials)

        # --- !!! REPLACE ABOVE WITH YOUR ACTUAL gspread AUTH METHOD !!! ---
        # --- Placeholder auth call ---
        gc = gspread.service_account_from_dict(creds_dict, scopes=scopes) # Requires gspread >= 5 (?)

        print("Successfully authenticated with Google Sheets API.")
        return gc
    except json.JSONDecodeError as e:
        print(f"[FATAL] Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[FATAL] Google Sheets authentication failed: {type(e).__name__} - {e}")
        sys.exit(1)

# --- Data Fetching Functions ---

def get_google_sheet_data(gc, sheet_id, worksheet_name):
    """Fetches all data from a specific worksheet in a Google Sheet."""
    print(f"Fetching data from Google Sheet ID '{sheet_id}', Worksheet '{worksheet_name}'...")
    try:
        spreadsheet = gc.open_by_id(sheet_id)
        worksheet = spreadsheet.worksheet(worksheet_name)
        data = worksheet.get_all_records() # Assumes first row is header
        print(f"Fetched {len(data)} rows.")
        return data
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"[ERROR] Google Sheet not found (ID: {sheet_id}). Check Sheet ID and permissions.")
        return []
    except gspread.exceptions.WorksheetNotFound:
        print(f"[ERROR] Worksheet '{worksheet_name}' not found in sheet ID {sheet_id}.")
        return []
    except Exception as e:
        print(f"[ERROR] Failed to fetch data from Google Sheet '{worksheet_name}': {type(e).__name__} - {e}")
        return []

def get_current_bans_from_reddit(subreddit):
    """Fetches the current list of banned users for a subreddit."""
    print(f"Fetching current ban list for r/{subreddit.display_name}...")
    bans = {}
    try:
        for ban_info in subreddit.banned():
            # Store the ban object itself to access note, date, etc.
            bans[ban_info.name.lower()] = ban_info
        print(f"Fetched {len(bans)} current bans.")
        return bans
    except prawcore.exceptions.Forbidden as e:
         print(f"[ERROR] Forbidden (403) fetching ban list for r/{subreddit.display_name}. Check bot's moderator permissions ('access'). {e}")
    except prawcore.exceptions.ResponseException as e:
         print(f"[ERROR] Reddit API error fetching ban list for r/{subreddit.display_name}: {type(e).__name__} - {e}")
    except Exception as e:
         print(f"[ERROR] Unexpected error fetching ban list for r/{subreddit.display_name}: {type(e).__name__} - {e}")
    return {} # Return empty dict on failure

def get_forgiveness_data():
    """
    Placeholder function to get users who should be explicitly unbanned or ignored.
    Implement based on how you track forgiveness (e.g., another sheet, a file, a list).
    """
    print("Fetching forgiveness data (using placeholder implementation)...")
    # --- USER IMPLEMENTATION REQUIRED ---
    # Example: return set(user.lower() for user in some_list)
    # Example: fetch from another sheet column
    return set() # Return empty set for now

# --- Helper Functions ---

# Cache mods per subreddit run to avoid repeated API calls
moderator_cache = {}
def is_mod(subreddit, username):
    """Checks if a user is a moderator of the subreddit (uses caching)."""
    sub_name_lower = subreddit.display_name.lower()
    if sub_name_lower not in moderator_cache:
        print(f"Fetching moderator list for r/{subreddit.display_name}...")
        try:
            moderator_cache[sub_name_lower] = {mod.name.lower() for mod in subreddit.moderator()}
            print(f"Cached {len(moderator_cache[sub_name_lower])} moderators.")
        except prawcore.exceptions.Forbidden as e:
            print(f"[ERROR] Forbidden (403) fetching mod list for r/{subreddit.display_name}. Check bot's 'access' permissions. {e}")
            moderator_cache[sub_name_lower] = set() # Cache empty set on permission error
        except Exception as e:
            print(f"[ERROR] Failed to fetch moderators for r/{subreddit.display_name}: {type(e).__name__} - {e}")
            moderator_cache[sub_name_lower] = set() # Cache empty set on other errors
            return False # Assume not mod if list fetch fails

    return username.lower() in moderator_cache.get(sub_name_lower, set())

# --- >>> PUBLIC LOGGING FUNCTION (NEEDS YOUR IMPLEMENTATION!) <<< ---
def log_public_action(action, username, target_sub, source_sub, actor, reason):
    """
    Logs a moderation action (BAN/UNBAN) to a public location (e.g., Google Sheet).
    THIS FUNCTION IS A PLACEHOLDER AND NEEDS TO BE IMPLEMENTED.
    """
    print(f"[Public Log] Action: {action}, User: {username}, Target: r/{target_sub}, Source: r/{source_sub}, Actor: {actor}, Reason: {reason}")

    # --- !!! USER IMPLEMENTATION REQUIRED HERE !!! ---
    # This is where you would add your code to write to the public Google Sheet
    # or other public logging destination.
    # Remember to include error handling (try...except) within this function.
    # Example structure:
    # try:
    #     # 1. Get Google Sheet client (maybe re-auth if needed, or use passed gc)
    #     # gc_public = authenticate_google() # Or use main gc if same permissions
    #     # 2. Open the correct public log sheet and worksheet
    #     # public_sheet = gc.open_by_id(GOOGLE_SHEET_ID_PUBLIC_LOG).worksheet(GOOGLE_SHEET_WORKSHEET_NAME_PUBLIC_LOG)
    #     # 3. Prepare the row data
    #     timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    #     log_row = [timestamp, action, username, target_sub, source_sub, actor, reason]
    #     # 4. Append the row
    #     # public_sheet.append_row(log_row, value_input_option='USER_ENTERED')
    #     print(f"[Public Log] Successfully logged action for {username}.") # Confirmation
    # except Exception as e:
    #     print(f"[ERROR] >>> Failed to write to public log for {username}: {type(e).__name__} - {e} <<<")
    # --- !!! END OF REQUIRED USER IMPLEMENTATION !!! ---

    pass # Remove this 'pass' once you add your implementation


# --- Main Processing Logic ---
def enforce_bans_on_sub(reddit, subreddit_name, rows_from_sheet, gc_main):
    """Processes the ban list rows against a specific subreddit."""
    global moderator_cache # Allow clearing cache for the sub
    moderator_cache.pop(subreddit_name.lower(), None) # Clear mod cache for this sub run

    print(f"\n--- Processing subreddit: r/{subreddit_name} ---")
    main_sheet_worksheet = None # Define before try block

    try:
        subreddit = reddit.subreddit(subreddit_name)
        current_bans = get_current_bans_from_reddit(subreddit)
        forgiven_users = get_forgiveness_data() # Get list of forgiven users

        # Try to get the main sheet worksheet for logging manual unbans (optional)
        if GOOGLE_SHEET_ID_MAIN:
             try:
                  main_sheet_worksheet = gc_main.open_by_id(GOOGLE_SHEET_ID_MAIN).worksheet(GOOGLE_SHEET_WORKSHEET_NAME_MAIN)
             except Exception as e:
                  print(f"[WARN] Could not open main Google Sheet '{GOOGLE_SHEET_WORKSHEET_NAME_MAIN}' for logging: {type(e).__name__} - {e}")
        else:
             print("[INFO] Main Google Sheet ID not configured, skipping logging of manual unbans to sheet.")

    except prawcore.exceptions.Redirect as e:
         print(f"[ERROR] Subreddit r/{subreddit_name} not found or redirected: {e}. Skipping.")
         return
    except prawcore.exceptions.Forbidden as e:
         print(f"[ERROR] Forbidden (403) accessing r/{subreddit_name}. Check bot permissions. Skipping. {e}")
         return
    except Exception as e:
        print(f"[ERROR] Failed to initialize processing for r/{subreddit_name}: {type(e).__name__} - {e}")
        return # Skip this subreddit if setup fails

    print(f"Processing {len(rows_from_sheet)} master list rows against r/{subreddit_name}...")
    processed_count = 0
    ban_count = 0
    unban_count = 0
    skip_count = 0

    for row_index, row in enumerate(rows_from_sheet):
        processed_count += 1
        user = None # Define user here for outer except block
        row_num_for_log = row_index + 2 # +1 for zero-index, +1 for header row

        try:
            # --- Data validation ---
            user = row.get('Username', '').strip()
            source_sub = row.get('SourceSub', '').strip()
            if not user or not source_sub:
                # Allow rows with missing data but log it clearly
                print(f"[INFO] Skipping sheet row #{row_num_for_log} due to missing Username or SourceSub: {row}")
                skip_count += 1
                continue
            # --- End Data validation ---

            user_lower = user.lower()
            already_banned_info = current_bans.get(user_lower) # Get ban object or None
            already_banned = already_banned_info is not None
            is_mod_user = is_mod(subreddit, user)
            is_exempt = user_lower in EXEMPT_USERS
            is_override = user_lower in forgiven_users # Check against forgiveness set

            # --- Forgiveness Logic ---
            if is_override:
                if already_banned:
                    try:
                        ban_reason_text = getattr(already_banned_info, "note", "") or "" # Safely get note
                        if CROSS_SUB_BAN_REASON.lower() in ban_reason_text.lower():
                            print(f"[ACTION] Attempting to unban {user} in r/{subreddit_name} (Forgiven)...")
                            subreddit.banned.remove(user)
                            unban_count += 1
                            # Update local state immediately
                            current_bans.pop(user_lower, None)
                            print(f"[UNBANNED] {user} in r/{subreddit_name} (Forgiven and ban reason matched)")
                            log_public_action("UNBANNED", user, subreddit_name, source_sub, "Bot", "Forgiven override")
                        # else:
                        #    print(f"[INFO] {user} in r/{subreddit_name} is forgiven, but existing ban note '{ban_reason_text}' doesn't match '{CROSS_SUB_BAN_REASON}'. Manual unban required if desired.")
                    except prawcore.exceptions.Forbidden as e:
                         print(f"[ERROR] Forbidden (403) unbanning {user} in r/{subreddit_name}. Check bot's ban permissions. {e}")
                    except prawcore.exceptions.ResponseException as e: # Catch other API errors
                         print(f"[ERROR] Reddit API error unbanning {user} in r/{subreddit_name}: {type(e).__name__} - {e}")
                    except Exception as e:
                        print(f"[ERROR] Failed to process forgiveness unban for {user} in r/{subreddit_name}: {type(e).__name__} - {e}")
                # else:
                #    print(f"[INFO] {user} is forgiven, but was not found in the ban list for r/{subreddit_name}.")
                skip_count += 1
                continue # Skip further processing for forgiven users

            # --- Manual Unban Detection ---
            # Check if user *should* be banned (present in sheet) but isn't on Reddit
            # Only log if they are *not* exempt/mod (as they shouldn't be banned anyway)
            if not already_banned and not is_exempt and not is_mod_user:
                manual_override_flag = str(row.get('ManualOverride', '')).strip().lower() in {'yes', 'true'}
                if not manual_override_flag:
                     # User is in the sheet, not banned, not exempt/mod, and no override flag set
                     # This *could* mean a manual unban happened OR they were never banned by the bot
                     print(f"[NOTICE] Potential manual unban detected for {user} in r/{subreddit_name} (User in sheet, not banned, no override).")
                     if main_sheet_worksheet:
                         try:
                             now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
                             # Ensure this list structure matches your main sheet columns
                             log_data = [user, source_sub, "Potential manual unban detected by bot", now, "", "", "", subreddit_name, now]
                             main_sheet_worksheet.append_row(log_data, value_input_option='USER_ENTERED')
                             print(f"[INFO] Logged potential manual unban for {user} to main Google Sheet.")
                         except Exception as e:
                             print(f"[ERROR] Failed to log potential manual unban for {user} to Google Sheet: {type(e).__name__} - {e}")
                     # Decide if you want to 'continue' here or let the ban logic proceed
                     # If you 'continue', users manually unbanned won't be automatically re-banned
                     # continue

            # --- Skip Conditions ---
            # Skip if already banned (and not handled by forgiveness), exempt, or a mod.
            if already_banned or is_exempt or is_mod_user:
                # Optional: Add print statements here for clarity if needed
                # print(f"[INFO] Skipping {user}: Already banned={already_banned}, Exempt={is_exempt}, Mod={is_mod_user}")
                skip_count += 1
                continue

            # --- Ban Logic ---
            # If we reach here, user is in the sheet, not banned, not exempt, not mod, not forgiven.
            try:
                print(f"[ACTION] Attempting to ban {user} in r/{subreddit_name} from source r/{source_sub}...")
                ban_note = f"Cross-sub ban from r/{source_sub}" # Note for moderators
                subreddit.banned.add(
                    user,
                    ban_reason=CROSS_SUB_BAN_REASON, # Reason shown to user (keep short)
                    note=ban_note
                )
                ban_count += 1
                # Update local state immediately (add placeholder BanInfo if needed)
                current_bans[user_lower] = {'name': user, 'note': ban_note} # Simulate BanInfo object
                print(f"[BANNED] {user} in r/{subreddit_name}")
                log_public_action("BANNED", user, subreddit_name, source_sub, "Bot", CROSS_SUB_BAN_REASON) # Pass ban reason

            except prawcore.exceptions.Forbidden as e:
                 print(f"[ERROR] Forbidden (403) banning {user} in r/{subreddit_name}. Check bot's ban permissions. {e}")
            except prawcore.exceptions.NotFound as e: # Catch user not found (deleted?)
                 print(f"[ERROR] User {user} not found (deleted or shadowbanned?), cannot ban in r/{subreddit_name}. {e}")
            except prawcore.exceptions.ResponseException as e: # Catch other API errors
                 print(f"[ERROR] Reddit API error banning {user} in r/{subreddit_name}: {type(e).__name__} - {e}")
            except Exception as e:
                print(f"[ERROR] Failed to ban {user} in r/{subreddit_name}: {type(e).__name__} - {e}")

        except Exception as e:
             # Catch errors processing a specific user row
             print(f"[ERROR] Unexpected error processing sheet row #{row_num_for_log} (User: {user or row.get('Username', '<unknown>')}, SourceSub: {row.get('SourceSub', '<unknown>')}): {type(e).__name__} - {e}")
             # Continue to the next row

    print(f"--- Finished processing r/{subreddit_name}: {ban_count} banned, {unban_count} unbanned, {skip_count} skipped/no action ---")


# --- Main Execution ---
def main():
    """Main function to run the bot."""
    start_time = datetime.now()
    print(f"Starting Cross-Sub Ban Bot at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Check for required environment variables early
    # Adapt check based on which set of REDDIT_ var names you use
    required_env_vars = ['CLIENT_ID', 'CLIENT_SECRET', 'USERNAME', 'PASSWORD', 'GOOGLE_SHEET_ID', 'GOOGLE_SERVICE_ACCOUNT_JSON']
    # required_env_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'REDDIT_USERNAME', 'REDDIT_PASSWORD', 'GOOGLE_SHEET_ID', 'GOOGLE_SERVICE_ACCOUNT_JSON'] # Uncomment if using REDDIT_ prefix
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
         print(f"[FATAL] Missing required environment variables: {', '.join(missing_vars)}")
         sys.exit(1)

    # --- Authenticate ---
    reddit = authenticate_reddit()
    gc = authenticate_google() # Google client for main sheet access

    if not reddit or not gc:
         print("[FATAL] Exiting due to authentication failure.")
         sys.exit(1)

    # --- Fetch Master Data ---
    # Fetch the master list of users to ban/manage FROM THE MAIN SHEET
    master_ban_list_rows = get_google_sheet_data(gc, GOOGLE_SHEET_ID_MAIN, GOOGLE_SHEET_WORKSHEET_NAME_MAIN)

    if not master_ban_list_rows:
         print("[WARN] Master ban list sheet appears empty or could not be fetched. No actions will be taken based on sheet data.")
         # Depending on logic, you might still want to run checks based on Reddit state?
         # For now, we'll exit if the primary source is empty.
         print(f"Script finished at {datetime.now()}. Total runtime: {datetime.now() - start_time}")
         sys.exit(0) # Exit cleanly


    # --- Process Subreddits ---
    # Loop through each target subreddit and enforce bans
    for sub_name in TARGET_SUBREDDITS:
        # Optional: Add delay between processing subreddits if needed for rate limits
        # time.sleep(2)
        enforce_bans_on_sub(reddit, sub_name, master_ban_list_rows, gc)

    end_time = datetime.now()
    print(f"\nCross-Sub Ban Bot finished at {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total runtime: {end_time - start_time}")

if __name__ == "__main__":
    main()
