import os
import json
import praw
import sqlite3
from contextlib import closing

# --- File Paths ---
SQLITE_DB_PATH = "bans.db"

# --- Load config.json ---
try:
    with open("config.json") as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

# --- Bot Settings ---
CROSS_SUB_BAN_REASON = config.get("CROSS_SUB_BAN_REASON", "Cross-sub ban policy")
EXEMPT_USERS = set(u.lower() for u in config.get("EXEMPT_USERS", []))
DAILY_BAN_LIMIT = config.get("DAILY_BAN_LIMIT", 100)
MAX_LOG_AGE_MINUTES = config.get("MAX_LOG_AGE_MINUTES", 60)
ROW_RETENTION_DAYS = config.get("ROW_RETENTION_DAYS", 30)
USE_SQLITE = True

# --- Load Trusted Subs ---
try:
    with open("trusted_subs.txt") as f:
        TRUSTED_SUBS = [line.strip().lower() for line in f if line.strip()]
except FileNotFoundError:
    TRUSTED_SUBS = []
TRUSTED_SOURCES = {f"r/{sub}" for sub in TRUSTED_SUBS}

# --- Database Wrapper ---
class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self._initialize_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _initialize_db(self):
        with closing(self._get_conn()) as con:
            con.execute('''
                CREATE TABLE IF NOT EXISTS bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    source_sub TEXT NOT NULL,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    manual_override TEXT DEFAULT 'no',
                    log_id TEXT,
                    moderator_name TEXT,
                    mod_sub TEXT,
                    forgive_timestamp DATETIME,
                    exempt_subs TEXT,
                    UNIQUE(username, source_sub)
                )
            ''')
            con.commit()

    def get_all_records(self):
        with closing(self._get_conn()) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM bans ORDER BY timestamp DESC").fetchall()
            return [dict(row) for row in rows]

    def append_row(self, row_data):
        try:
            with closing(self._get_conn()) as con:
                con.execute(
                    "INSERT OR IGNORE INTO bans (username, source_sub, reason, timestamp, log_id, moderator_name) VALUES (?, ?, ?, ?, ?, ?)",
                    (row_data[0], row_data[1], row_data[2], row_data[3], row_data[5], row_data[6])
                )
                con.commit()
            return True
        except Exception as e:
            print(f"[DB_ERROR] Failed to append row: {e}")
            return False

    def update_forgiveness(self, username, source, mod, sub, forgive_time):
        try:
            with closing(self._get_conn()) as con:
                con.execute(
                    """
                    UPDATE bans SET manual_override = 'yes', moderator_name = ?, mod_sub = ?, forgive_timestamp = ?
                    WHERE lower(username) = ? AND source_sub = ?
                    """,
                    (mod, sub, forgive_time, username.lower(), source)
                )
                con.commit()
            return True
        except Exception as e:
            print(f"[DB_ERROR] Failed to update forgiveness: {e}")
            return False

    def get_recent_entries(self, source_sub, hours=24):
        with closing(self._get_conn()) as con:
            cursor = con.execute(
                "SELECT COUNT(*) FROM bans WHERE source_sub = ? AND timestamp > datetime('now', ?)",
                (source_sub, f'-{hours} hours')
            )
            return cursor.fetchone()[0]

    def cleanup_old_records(self, days):
        with closing(self._get_conn()) as con:
            cursor = con.cursor()
            cursor.execute("DELETE FROM bans WHERE timestamp < datetime('now', ?)", (f'-{days} days',))
            deleted_count = cursor.rowcount
            con.commit()
            return deleted_count

# --- Reddit API Setup ---
def setup_reddit():
    return praw.Reddit(
        client_id=os.environ['REDDIT_CLIENT_ID'],
        client_secret=os.environ['REDDIT_CLIENT_SECRET'],
        username=os.environ['REDDIT_USERNAME'],
        password=os.environ['REDDIT_PASSWORD'],
        user_agent='CrossSubBanBot/2.0 (by u/your_username)'
    )

# --- Instantiate APIs ---
reddit = setup_reddit()
database = Database(SQLITE_DB_PATH)
