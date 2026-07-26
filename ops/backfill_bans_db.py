#!/opt/cross_sub_ban_bot/.venv/bin/python
"""
backfill_bans_db.py — rebuild bans.db from public_ban_log.json.

Why: ROW_RETENTION_DAYS=10 (since fixed to 3650) wiped bans.db to 0
rows. The full history survives in public_ban_log.json. The DB stores
ORIGIN bans as unique (username, source_sub) — propagation to target
subs is derived, not stored — so we dedup the log's per-target
propagation events back to one row per (user, source_sub), using the
EARLIEST timestamp seen for that pair.

Forgiveness: an UNBANNED/FORGIVEN event for a (user, source_sub) that
post-dates its ban marks that row manual_override='yes'.

Safety:
  - Writes to a NEW file (bans.db.rebuilt) and only swaps it in after
    a sanity check, keeping a timestamped backup of the old DB.
  - Idempotent: re-running reproduces the same result.
  - moderator_name is left '' (privacy; not in the log anyway).
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BOT_DIR = "/opt/cross_sub_ban_bot"
LOG = os.path.join(BOT_DIR, "public_ban_log.json")
DB = os.path.join(BOT_DIR, "bans.db")
NEW = DB + ".rebuilt"

SCHEMA = """
CREATE TABLE bans (
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
"""

REASON = "Auto XSub Pact Ban"


def norm_sub(s):
    return (s or "").strip().lower().lstrip("r/").strip("/")


def main():
    with open(LOG) as f:
        events = json.load(f)

    # Collapse to origin bans: key = (username_lower, source_sub_norm)
    # value = earliest ban timestamp. Track forgiveness separately.
    bans = {}          # key -> earliest ts str
    orig_user = {}     # key -> original-cased username (for display)
    forgives = {}      # key -> latest forgive ts str

    for e in events:
        action = (e.get("action") or "").upper()
        user = e.get("username", "")
        key = (user.lower(), norm_sub(e.get("source_sub")))
        ts = e.get("timestamp", "")
        if not user or not key[1]:
            continue
        if action == "BANNED":
            if key not in bans or ts < bans[key]:
                bans[key] = ts
                orig_user.setdefault(key, user)
            orig_user.setdefault(key, user)
        elif action in ("UNBANNED", "FORGIVEN"):
            if key not in forgives or ts > forgives[key]:
                forgives[key] = ts

    # Build the new DB
    if os.path.exists(NEW):
        os.unlink(NEW)
    con = sqlite3.connect(NEW)
    con.execute(SCHEMA)

    rows = 0
    forgiven_rows = 0
    for key, ts in sorted(bans.items(), key=lambda kv: kv[1]):
        user_disp = orig_user.get(key, key[0])
        src = "r/" + key[1]
        # A forgive only counts if it post-dates the ban
        f_ts = forgives.get(key)
        is_forgiven = f_ts is not None and f_ts >= ts
        con.execute(
            "INSERT OR IGNORE INTO bans "
            "(username, source_sub, reason, timestamp, manual_override, "
            " log_id, moderator_name, forgive_timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_disp, src, REASON, ts,
             "yes" if is_forgiven else "no",
             "", "", f_ts if is_forgiven else None),
        )
        rows += 1
        if is_forgiven:
            forgiven_rows += 1
    con.commit()

    # Sanity check
    n = con.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
    con.close()
    print(f"[backfill] built {NEW}: {n} rows ({forgiven_rows} forgiven)")

    if n != rows:
        print(f"[backfill] ABORT: row mismatch {n} != {rows}")
        sys.exit(1)
    if n < 100 or n > 5000:
        print(f"[backfill] ABORT: sanity bound failed (n={n}, expected ~800)")
        sys.exit(1)

    # Swap in with backup
    if os.path.exists(DB):
        bak = DB + ".bak." + datetime.now().strftime("%Y%m%d-%H%M%S")
        os.rename(DB, bak)
        print(f"[backfill] backed up old DB -> {bak}")
    os.rename(NEW, DB)
    print(f"[backfill] installed rebuilt bans.db ({n} rows)")


if __name__ == "__main__":
    main()
