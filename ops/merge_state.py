#!/opt/cross_sub_ban_bot/.venv/bin/python
"""
merge_state.py - reconcile this executor's bot state with the remote's.

WHY THIS EXISTS
---------------
The bot runs in two places (the shkn.ws systemd timer, and GitHub Actions
as failover). Both commit generated state to main. When they diverge,
`git pull --rebase` cannot resolve it:

  * bans.db is a BINARY SQLite file - git has no merge strategy for it,
    so every divergence is a guaranteed conflict.
  * A conflicted rebase leaves .git/rebase-merge behind. The next run
    then fails with "cannot pull with rebase: unmerged files", commits
    pile up unpushed, and the repo can end up on a detached HEAD - at
    which point `git push` fails permanently and the server looks dead
    to the failover guard. That is exactly the outage of 2026-07-27.

Picking a winner is also wrong: both executors read real modlogs, so
each may hold bans the other doesn't. Discarding either side loses
moderation data.

WHAT THIS DOES
--------------
Given a snapshot of OUR state and a checkout reset to the REMOTE state,
merge ours into theirs semantically, per file type:

  bans.db              union of rows, keyed by UNIQUE(username, source_sub);
                       forgiveness wins over non-forgiveness
  public_ban_log.json  union of events, deduped, sorted by timestamp
  pending_subs.json    dict merge; 'welcomed'/'perms_asked' lists unioned
  bot_health.json      ours (regenerated every run; ephemeral)
  trusted_subs.txt     ours (discovery derives it from Reddit each run)
  public_ban_log.md    ours (derived artifacts, rewritten by the bot)
  public_ban_log.html  ours

Because the working tree is hard-reset to the remote before this runs,
there is never a git-level merge - so there is never a conflict, and
never a stuck rebase.

Usage: merge_state.py <ours_snapshot_dir> <repo_dir>
"""
import json
import os
import shutil
import sqlite3
import sys

OURS_WINS = (
    # bot_health.json omitted: gitignored, never in a checkout to merge.
    "trusted_subs.txt",
    "public_ban_log.md",
    "public_ban_log.html",
)


def log(msg):
    print(f"[MERGE] {msg}", flush=True)


def merge_bans_db(ours_path, theirs_path):
    """
    Union our ban rows into the remote DB. The UNIQUE(username, source_sub)
    constraint makes insertion idempotent; INSERT OR IGNORE therefore adds
    exactly the rows the remote is missing.

    Forgiveness is reconciled separately: if either side has marked a ban
    forgiven, the merged row is forgiven. An unban is a deliberate mod
    action and must not be silently undone by a merge.
    """
    if not os.path.exists(ours_path):
        return 0, 0
    if not os.path.exists(theirs_path):
        shutil.copy2(ours_path, theirs_path)
        log("remote had no bans.db; used ours wholesale")
        return 0, 0

    con = sqlite3.connect(theirs_path)
    try:
        con.execute("ATTACH DATABASE ? AS ours", (ours_path,))
    except sqlite3.Error as e:
        log(f"ERROR attaching our bans.db: {e} - leaving remote DB as-is")
        con.close()
        return 0, 0

    before = con.execute("SELECT COUNT(*) FROM bans").fetchone()[0]

    con.execute("""
        INSERT OR IGNORE INTO bans
            (username, source_sub, reason, timestamp, manual_override,
             log_id, moderator_name, mod_sub, forgive_timestamp, exempt_subs)
        SELECT username, source_sub, reason, timestamp, manual_override,
               log_id, moderator_name, mod_sub, forgive_timestamp, exempt_subs
        FROM ours.bans
    """)

    forgiven = con.execute("""
        UPDATE bans
           SET manual_override = 'yes',
               forgive_timestamp = COALESCE(
                   bans.forgive_timestamp,
                   (SELECT o.forgive_timestamp FROM ours.bans o
                     WHERE lower(o.username) = lower(bans.username)
                       AND lower(o.source_sub) = lower(bans.source_sub)))
         WHERE lower(manual_override) != 'yes'
           AND EXISTS (SELECT 1 FROM ours.bans o
                        WHERE lower(o.username) = lower(bans.username)
                          AND lower(o.source_sub) = lower(bans.source_sub)
                          AND lower(o.manual_override) = 'yes')
    """).rowcount

    con.commit()
    after = con.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
    con.execute("DETACH DATABASE ours")
    con.close()

    added = after - before
    log(f"bans.db: {before} remote + ours -> {after} "
        f"(+{added} rows, {forgiven} forgiveness updates)")
    return added, forgiven


def merge_ban_log(ours_path, theirs_path):
    """Union the append-only event log, deduped and time-ordered."""
    def load(p):
        try:
            with open(p) as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    ours, theirs = load(ours_path), load(theirs_path)
    if not ours:
        return 0

    def key(e):
        return (e.get("timestamp"), e.get("action"), e.get("username"),
                e.get("subreddit"), e.get("source_sub"))

    merged = {key(e): e for e in theirs}
    before = len(merged)
    for e in ours:
        merged.setdefault(key(e), e)

    out = sorted(merged.values(), key=lambda e: str(e.get("timestamp") or ""))
    with open(theirs_path, "w") as f:
        json.dump(out, f, indent=2)

    added = len(out) - before
    log(f"public_ban_log.json: {before} remote -> {len(out)} (+{added} events)")
    return added


def merge_pending(ours_path, theirs_path):
    """Merge onboarding bookkeeping; union the tracking lists."""
    def load(p):
        try:
            with open(p) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    ours, theirs = load(ours_path), load(theirs_path)
    if not ours:
        return

    merged = dict(theirs)
    merged.setdefault("pending", {}).update(ours.get("pending", {}))
    for list_key in ("welcomed", "perms_asked"):
        combined = list(dict.fromkeys(
            list(theirs.get(list_key, [])) + list(ours.get(list_key, []))))
        if combined:
            merged[list_key] = combined

    with open(theirs_path, "w") as f:
        json.dump(merged, f, indent=2)
    log("pending_subs.json: merged (lists unioned)")


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    ours_dir, repo_dir = sys.argv[1], sys.argv[2]

    merge_bans_db(os.path.join(ours_dir, "bans.db"),
                  os.path.join(repo_dir, "bans.db"))
    merge_ban_log(os.path.join(ours_dir, "public_ban_log.json"),
                  os.path.join(repo_dir, "public_ban_log.json"))
    merge_pending(os.path.join(ours_dir, "pending_subs.json"),
                  os.path.join(repo_dir, "pending_subs.json"))

    for name in OURS_WINS:
        src = os.path.join(ours_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(repo_dir, name))
    log(f"took ours for: {', '.join(OURS_WINS)}")
    log("state reconciled")


if __name__ == "__main__":
    main()
