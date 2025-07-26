import os
import json
import base64
import gspread
import praw
from oauth2client.service_account import ServiceAccountCredentials

# --- Directory and log paths ---
WORK_DIR = "/home/runner/work/cross_sub_ban_bot/cross_sub_ban_bot"
PUBLIC_LOG_JSON = f"{WORK_DIR}/public_ban_log.json"
PUBLIC_LOG_MD = f"{WORK_DIR}/public_ban_log.md"

# --- Load config.json ---
with open("config.json") as f:
    config = json.load(f)

CROSS_SUB_BAN_REASON   = config.get("CROSS_SUB_BAN_REASON", "Auto XSub Pact Ban")
EXEMPT_USERS           = set(u.lower() for u in config.get("EXEMPT_USERS", []))
DAILY_BAN_LIMIT        = config.get("DAILY_BAN_LIMIT", 50)
MAX_LOG_AGE_MINUTES    = config.get("MAX_LOG_AGE_MINUTES", 600)
ROW_RETENTION_DAYS     = config.get("ROW_RETENTION_DAYS", 10)

# --- NEW: Database backend selection ---
USE_SQLITE = config.get("USE_SQLITE", False)  # Set to True to use SQLite instead of Google Sheets
SQLITE_DB_PATH = config.get("SQLITE_DB_PATH", "bans.db")

# --- Load trusted subs from file ---
def load_trusted_subs(path="trusted_subs.txt"):
    with open(path) as f:
        return [line.strip().lower() for line in f if line.strip()]

TRUSTED_SUBS    = load_trusted_subs()
TRUSTED_SOURCES = {f"r/{sub}" for sub in TRUSTED_SUBS}

# --- Google Sheets setup ---
def setup_google_sheet():
    creds_env = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not creds_env:
        raise SystemExit("[FATAL] Missing GOOGLE_SERVICE_ACCOUNT_JSON env var.")

    try:
        decoded = base64.b64decode(creds_env)
        creds_str = decoded.decode('utf-8')
        creds_dict = json.loads(creds_str)
    except Exception:
        creds_dict = json.loads(creds_env)

    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)

    sheet_key = os.environ.get('GOOGLE_SHEET_ID')
    if not sheet_key:
        raise SystemExit("[FATAL] Missing GOOGLE_SHEET_ID env var.")

    sheet = client.open_by_key(sheet_key).sheet1
    print(f"[INFO] Google Sheet '{sheet_key}' opened, worksheet '{sheet.title}' loaded.")
    return sheet, client, sheet_key

# --- SQLite setup ---
def setup_sqlite_database():
    """Setup SQLite database as replacement for Google Sheets"""
    from db_utils import BanDatabase
    print(f"[INFO] SQLite database '{SQLITE_DB_PATH}' initialized.")
    return BanDatabase(SQLITE_DB_PATH)

# --- Database abstraction layer ---
class DatabaseWrapper:
    """
    Wrapper class that provides the same interface as Google Sheets
    but can use either Google Sheets or SQLite backend
    """
    def __init__(self, use_sqlite=False):
        if use_sqlite:
            print("[INFO] Using SQLite database backend")
            self.db = setup_sqlite_database()
            self.backend = 'sqlite'
        else:
            print("[INFO] Using Google Sheets backend")
            self.sheet, self.client, self.sheet_key = setup_google_sheet()
            self.backend = 'sheets'
    
    def get_all_records(self):
        """Get all records from database"""
        if self.backend == 'sqlite':
            return self.db.get_all_records()
        else:
            return self.sheet.get_all_records()
    
    def append_row(self, row_data, value_input_option='USER_ENTERED'):
        """Add new row to database"""
        if self.backend == 'sqlite':
            return self.db.append_row(row_data)
        else:
            return self.sheet.append_row(row_data, value_input_option=value_input_option)
    
    def update_cell(self, row, col, value):
        """Update a specific cell - SQLite uses different approach"""
        if self.backend == 'sqlite':
            # SQLite doesn't use cell-by-cell updates
            # This will be handled by specific update methods
            print(f"[WARNING] update_cell() called on SQLite backend - use specific update methods instead")
            return True
        else:
            return self.sheet.update_cell(row, col, value)
    
    def update_forgiveness(self, username, source_sub, moderator, mod_sub, forgive_time):
        """Update user forgiveness status"""
        if self.backend == 'sqlite':
            return self.db.update_forgiveness(username, source_sub, moderator, mod_sub, forgive_time)
        else:
            # For Google Sheets, we need to find the row and update multiple cells
            # This is more complex and slower
            print("[WARNING] update_forgiveness() not implemented for Google Sheets backend")
            return False
    
    def update_exempt_subs(self, username, exempt_subs):
        """Update exempt subreddits for a user"""
        if self.backend == 'sqlite':
            return self.db.update_exempt_subs(username, exempt_subs)
        else:
            print("[WARNING] update_exempt_subs() not implemented for Google Sheets backend")
            return False
    
    def cleanup_old_records(self, retention_days):
        """Clean up old records"""
        if self.backend == 'sqlite':
            return self.db.cleanup_old_records(retention_days)
        else:
            print("[WARNING] cleanup_old_records() not implemented for Google Sheets backend")
            return 0
    
    def get_stats(self):
        """Get database statistics"""
        if self.backend == 'sqlite':
            return self.db.get_stats()
        else:
            # Basic stats for Google Sheets
            records = self.get_all_records()
            return {
                'total_records': len(records),
                'backend': 'google_sheets'
            }

# --- Reddit API setup ---
def setup_reddit():
    return praw.Reddit(
        client_id=os.environ.get('REDDIT_CLIENT_ID') or os.environ.get('CLIENT_ID'),
        client_secret=os.environ.get('REDDIT_CLIENT_SECRET') or os.environ.get('CLIENT_SECRET'),
        username=os.environ.get('REDDIT_USERNAME') or os.environ.get('USERNAME'),
        password=os.environ.get('REDDIT_PASSWORD') or os.environ.get('PASSWORD'),
        user_agent='Cross-Sub Ban Bot/1.0'
    )

# --- Instantiate API clients here ---
print(f"[CONFIG] Database backend: {'SQLite' if USE_SQLITE else 'Google Sheets'}")

# Create database wrapper
database = DatabaseWrapper(use_sqlite=USE_SQLITE)

# For backward compatibility, expose as 'sheet'
if USE_SQLITE:
    sheet = database  # The wrapper provides the same interface
    client = None
    sheet_key = SQLITE_DB_PATH
else:
    sheet = database.sheet
    client = database.client
    sheet_key = database.sheet_key

# Always create reddit client
reddit = setup_reddit()
