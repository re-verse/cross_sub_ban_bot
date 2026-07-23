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
                # moderator_name is deliberately NOT stored (privacy):
                # this repo is public, so bans.db is downloadable by the
                # very users it records. The origin sub's own private
                # modlog remains the authoritative record of which mod
                # acted; the pact only needs to know the source sub.
                con.execute(
                    "INSERT OR IGNORE INTO bans (username, source_sub, reason, timestamp, log_id, moderator_name) VALUES (?, ?, ?, ?, ?, ?)",
                    (row_data[0], row_data[1], row_data[2], row_data[3], row_data[5], '')
                )
                con.commit()
            return True
        except Exception as e:
            print(f"[DB_ERROR] Failed to append row: {e}")
            return False

    def update_forgiveness(self, username, source, mod, sub, forgive_time):
        try:
            with closing(self._get_conn()) as con:
                # mod name not stored — see append_row privacy note.
                con.execute(
                    """
                    UPDATE bans SET manual_override = 'yes', moderator_name = '', mod_sub = ?, forgive_timestamp = ?
                    WHERE lower(username) = ? AND source_sub = ?
                    """,
                    (sub, forgive_time, username.lower(), source)
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

    # -- Modmail-driven lookups & mutations ----------------------------------

    def find_user_records(self, username):
        """All ban records for a username (case-insensitive). Most recent first."""
        try:
            with closing(self._get_conn()) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT * FROM bans WHERE lower(username) = ? "
                    "ORDER BY timestamp DESC",
                    (username.lower(),),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            print(f"[DB_ERROR] find_user_records({username}): {e}")
            return []

    def apply_pardon(self, username, source_sub, mod, mod_sub, when):
        """
        Mark a (username, source_sub) row as pardoned. Same effect as the
        modlog-driven unban: sets manual_override='yes' and records where
        and when. The pardoning mod's name is NOT stored — see the
        privacy note on append_row. Returns True if a row was updated.
        """
        try:
            with closing(self._get_conn()) as con:
                cur = con.execute(
                    "UPDATE bans SET manual_override = 'yes', "
                    "moderator_name = '', mod_sub = ?, forgive_timestamp = ? "
                    "WHERE lower(username) = ? AND lower(source_sub) = ?",
                    (mod_sub, when, username.lower(), source_sub.lower()),
                )
                con.commit()
                return cur.rowcount > 0
        except Exception as e:
            print(f"[DB_ERROR] apply_pardon({username}, {source_sub}): {e}")
            return False

    def get_exempt_subs(self, username):
        """
        Union of exempt subs across all of this user's rows, normalized
        to bare lowercase sub names (no r/ prefix). Used at propagation
        time — a user exempted in r/X via /xsub exempt must never be
        banned in r/X by the pact, regardless of which sub the ban
        originated from.
        """
        subs = set()
        try:
            with closing(self._get_conn()) as con:
                rows = con.execute(
                    "SELECT exempt_subs FROM bans WHERE lower(username) = ?",
                    (username.lower(),),
                ).fetchall()
            for (field,) in rows:
                for part in (field or "").lower().split(","):
                    part = part.strip().lstrip("r/").strip("/")
                    if part:
                        subs.add(part)
        except Exception as e:
            print(f"[DB_ERROR] get_exempt_subs({username}): {e}")
        return subs

    def add_exemption(self, row_id, exempt_sub):
        """
        Append a sub to the exempt_subs CSV on a single row, deduped and
        case-folded. Returns True on success.
        """
        exempt_sub = exempt_sub.lower().lstrip("r/").strip("/")
        if not exempt_sub:
            return False
        try:
            with closing(self._get_conn()) as con:
                con.row_factory = sqlite3.Row
                row = con.execute(
                    "SELECT exempt_subs FROM bans WHERE id = ?", (row_id,)
                ).fetchone()
                if row is None:
                    return False
                current = (row["exempt_subs"] or "").lower()
                parts = {p.strip() for p in current.split(",") if p.strip()}
                if exempt_sub in parts:
                    return True  # already exempt, no-op success
                parts.add(exempt_sub)
                new_field = ", ".join(sorted(parts))
                con.execute(
                    "UPDATE bans SET exempt_subs = ? WHERE id = ?",
                    (new_field, row_id),
                )
                con.commit()
                return True
        except Exception as e:
            print(f"[DB_ERROR] add_exemption(id={row_id}, {exempt_sub}): {e}")
            return False

# --- Reddit API Setup ---
def setup_reddit():
    return praw.Reddit(
        client_id=os.environ['REDDIT_CLIENT_ID'],
        client_secret=os.environ['REDDIT_CLIENT_SECRET'],
        username=os.environ['REDDIT_USERNAME'],
        password=os.environ['REDDIT_PASSWORD'],
        user_agent='CrossSubBanBot/2.0 (by u/re-verse)'
    )

# --- Instantiate APIs ---
reddit = setup_reddit()
database = Database(SQLITE_DB_PATH)

# --- Owner Alerts ---
OWNER_USERNAME = config.get("OWNER_USERNAME", "re-verse")


def notify_owner(subject, body, dry_run=False):
    """
    Send a DM to the bot owner. Used by the health tracker to flag
    sub-level access regressions in near-real-time.

    Returns True on success (or dry-run skip), False on error. Failures
    are logged but never raised — alerting must not break the cron tick.
    """
    if dry_run:
        print(f"[DRY-RUN][NOTIFY-OWNER] would DM u/{OWNER_USERNAME}: {subject}")
        return True
    try:
        reddit.redditor(OWNER_USERNAME).message(subject=subject, message=body)
        print(f"[NOTIFY-OWNER] DM sent to u/{OWNER_USERNAME}: {subject}")
        return True
    except Exception as e:
        print(f"[NOTIFY-OWNER-ERROR] Could not DM u/{OWNER_USERNAME}: {e}")
        return False
