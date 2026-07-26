import os
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
    reddit,
    notify_owner,
)
from log_utils import log_public_action, flush_views, log_ban_event
from health_utils import (
    load_health,
    save_health,
    record_success,
    record_failure,
    summary as health_summary,
    dispatch_alerts,
    maybe_alert_cron_gap,
)
from modmail_utils import check_modmail
from inbox_utils import check_dm_inbox
from subreddit_discovery import (
    discover, load_allowlist, load_trusted, save_trusted,
    load_pending, save_pending,
)

# --- Global Cache ---
BAN_CACHE = []
HEALTH_STATE = None  # populated in main()

# When DRY_RUN is set, propagation calls log intent but don't hit Reddit.
DRY_RUN = os.environ.get('DRY_RUN', '').lower() in ('1', 'true', 'yes')
if DRY_RUN:
    print("[DRY-RUN] No bans/unbans will be applied. Detection + DB writes still happen.")

# --- Helper Functions ---
def is_forgiven(username, cache):
    """Check if a user has been manually forgiven."""
    for record in cache:
        if record.get('username', '').lower() == username.lower():
            if record.get('manual_override', '').lower() == 'yes':
                return True
    return False

def apply_ban_across_network(username, source_sub, actor="Bot", note=None, moderator=None):
    """
    Apply a ban to every trusted sub except the originating one.

    actor: who to attribute the public-log entries to. Defaults to 'Bot'
        for normal modlog-driven propagation. Pass a string like
        'pact-owner (super-ban)' for manually-initiated bans.
    note: ban note shown in modmail to the banned user. Defaults to the
        standard 'NHL subs share a pact' note. Pass a custom string for
        super-bans or other special cases.

    When source_sub doesn't match any TRUSTED_SUB (e.g. 'manual'), the
    skip clause is a no-op and all 9 subs get the ban — used by the
    /xsub super ban modmail command.
    """
    if note is None:
        note = (
            f"Cross-sub ban from {source_sub}. NHL subs share a pact to fight trolling. "
            f"To appeal, message mods of {source_sub}."
        )
    src_lc = source_sub.lower().lstrip('r/').strip('/')
    # Per-sub exemptions granted via /xsub exempt. Checked here — at
    # propagation time — so an exemption holds no matter which sub the
    # ban originated from.
    exempt_in = database.get_exempt_subs(username)
    banned_count = 0
    for sub in TRUSTED_SUBS:
        if sub.lower() == src_lc:
            continue
        if sub.lower() in exempt_in:
            print(f"[SKIP-EXEMPT] u/{username} is exempt in r/{sub}, not banning there.")
            continue
        if DRY_RUN:
            print(f"[DRY-RUN][PROPAGATE-BAN] would ban u/{username} in r/{sub} (from {source_sub}, actor={actor})")
            continue
        try:
            sr = reddit.subreddit(sub)
            sr.banned.add(username, ban_reason=CROSS_SUB_BAN_REASON, note=note)
            print(f"[PROPAGATE-BAN] u/{username} -> r/{sub} (from {source_sub}, actor={actor})")
            log_public_action("BANNED", username, sub, source_sub=source_sub, actor=actor)
            banned_count += 1
        except prawcore.exceptions.Forbidden:
            print(f"[WARN] No ban permission in r/{sub}, skipping.")
        except Exception as e:
            print(f"[ERROR] Failed to ban u/{username} in r/{sub}: {e}")
    log_ban_event("BANNED", moderator, username, source_sub, banned_count, len(TRUSTED_SUBS) - 1)
    return banned_count

def apply_unban_across_network(username, source_sub):
    """Remove a ban from every trusted sub except the originating one."""
    src_lc = source_sub.lower().lstrip('r/').strip('/')
    for sub in TRUSTED_SUBS:
        if sub.lower() == src_lc:
            continue
        if DRY_RUN:
            print(f"[DRY-RUN][PROPAGATE-UNBAN] would unban u/{username} in r/{sub} (from {source_sub})")
            continue
        try:
            sr = reddit.subreddit(sub)
            sr.banned.remove(username)
            print(f"[PROPAGATE-UNBAN] u/{username} -> r/{sub} (from {source_sub})")
            log_public_action("UNBANNED", username, sub, source_sub=source_sub, actor="Bot")
        except prawcore.exceptions.NotFound:
            pass  # not banned in target sub, fine
        except prawcore.exceptions.Forbidden:
            print(f"[WARN] No ban permission in r/{sub}, skipping.")
        except Exception as e:
            print(f"[ERROR] Failed to unban u/{username} in r/{sub}: {e}")

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
                break  # Modlog is chronological, so we can stop early

            user = getattr(log, "target_author", None)
            if not user or ' ' in user:
                continue

            user_lc = user.lower()
            source = f"r/{log.subreddit}".lower()
            # log.mod can be None if the acting moderator's account was
            # deleted; don't let that crash the whole sub's sync pass.
            mod_name = getattr(log.mod, 'name', None) or '[deleted]'

            if log.action == "banuser":
                # Only act on bans whose reason matches the pact reason
                desc = (getattr(log, 'description', '') or '').lower()
                if CROSS_SUB_BAN_REASON.lower() not in desc:
                    continue
                handle_ban_action(user, user_lc, source, mod_name, sub, log.created_utc, log.id)
            elif log.action == "unbanuser":
                handle_unban_action(user, user_lc, source, mod_name, sub, log.created_utc)

        # Modlog walked successfully — record the sub as healthy
        if HEALTH_STATE is not None:
            record_success(HEALTH_STATE, sub)

    except prawcore.exceptions.Forbidden as e:
        print(f"[ERROR] Access forbidden for r/{sub}: {e}")
        if HEALTH_STATE is not None:
            record_failure(HEALTH_STATE, sub, f"Forbidden: {e}")
    except prawcore.exceptions.NotFound as e:
        print(f"[ERROR] NotFound for r/{sub}: {e}")
        if HEALTH_STATE is not None:
            record_failure(HEALTH_STATE, sub, f"NotFound: {e}")
    except Exception as e:
        print(f"[ERROR] Failed to process r/{sub}: {e}")
        traceback.print_exc()
        if HEALTH_STATE is not None:
            record_failure(HEALTH_STATE, sub, e)

def handle_unban_action(user, user_lc, source, mod, sub, timestamp):
    """Process an unban action: forgive in DB, propagate the unban."""
    # Only forgive when the unban happens IN the originating sub
    matching = [
        r for r in BAN_CACHE
        if r.get('username', '').lower() == user_lc
        and r.get('source_sub', '').lower() == source
    ]
    if not matching:
        return

    print(f"[UNBAN] Detected unban for u/{user} in {source}.")
    forgiven_time = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
    if database.update_forgiveness(user, source, mod, sub, forgiven_time):
        print(f"[FORGIVE] Marked u/{user} as forgiven in the database.")
        for record in BAN_CACHE:
            if record.get('username', '').lower() == user_lc and record.get('source_sub', '').lower() == source:
                record['manual_override'] = 'yes'
                break
        # Propagate the unban across the network
        apply_unban_across_network(user, source)

def handle_ban_action(user, user_lc, source, mod, sub, timestamp, log_id):
    """Process a ban action: log it, propagate it."""
    if user_lc in EXEMPT_USERS:
        print(f"[SKIP] User u/{user} is exempt.")
        return

    if is_forgiven(user, BAN_CACHE):
        print(f"[SKIP] User u/{user} has a manual override (forgiven).")
        return

    if any(r.get('username', '').lower() == user_lc and r.get('source_sub', '').lower() == source for r in BAN_CACHE):
        return  # already in DB, already propagated on a previous run

    recent_count = database.get_recent_entries(source, hours=24)
    if recent_count >= DAILY_BAN_LIMIT:
        print(f"[SKIP] Daily limit ({DAILY_BAN_LIMIT}) reached for {source}.")
        return

    print(f"[BAN] Detected pact ban for u/{user} in {source}. Logging + propagating.")
    row_data = [
        user,
        source,
        CROSS_SUB_BAN_REASON,
        datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S'),
        '',  # manual_override
        log_id,
        mod
    ]
    if database.append_row(row_data):
        BAN_CACHE.append({'username': user, 'source_sub': source, 'manual_override': 'no'})
        # Propagate the ban across the network
        apply_ban_across_network(user, source, moderator=mod)

def main():
    """Main bot execution function."""
    global HEALTH_STATE
    print("="*60)
    print("Cross-Sub Ban Bot - SQLite Edition")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    HEALTH_STATE = load_health()

    # Detect scheduler degradation: compares now to the PREVIOUS run's
    # last_run stamp (bumped only at end-of-run), DMs owner if the gap
    # exceeded the threshold. Throttled inside.
    try:
        maybe_alert_cron_gap(HEALTH_STATE, notify_owner, dry_run=DRY_RUN)
    except Exception as e:
        print(f"[ERROR] maybe_alert_cron_gap raised: {e}")

    try:
        print("\n[PHASE 1] Loading Data")
        load_ban_cache()

        print("\n[PHASE 2] Syncing Bans from Source Subreddits")
        for sub in TRUSTED_SUBS:
            sync_bans_from_sub(sub)

        print("\n[PHASE 2.5] Checking Modmail for /xsub commands")
        try:
            check_modmail(
                health_state=HEALTH_STATE,
                dry_run=DRY_RUN,
                propagate_unban=apply_unban_across_network,
                propagate_ban=apply_ban_across_network,
            )
        except Exception as e:
            print(f"[ERROR] Modmail check raised: {e}")
            traceback.print_exc()

        print("\n[PHASE 2.6] Checking DM inbox for /xsub help")
        try:
            check_dm_inbox(dry_run=DRY_RUN)
        except Exception as e:
            print(f"[ERROR] DM inbox check raised: {e}")
            traceback.print_exc()

        print("\n[PHASE 2.7] Subreddit discovery + auto-onboarding")
        try:
            bot_name = (os.environ.get("REDDIT_USERNAME", "") or "").lower()
            if not bot_name:
                try:
                    bot_name = reddit.user.me().name.lower()
                except Exception:
                    pass
            trusted_before = load_trusted()
            allowlist = load_allowlist()
            pending = load_pending()
            trusted_after = discover(
                reddit, bot_name, list(trusted_before), allowlist, pending,
                notify_func=notify_owner, dry_run=DRY_RUN,
            )
            if not DRY_RUN:
                if trusted_after != trusted_before:
                    save_trusted(trusted_after)
                save_pending(pending)
        except Exception as e:
            print(f"[ERROR] Subreddit discovery raised: {e}")
            traceback.print_exc()

        print("\n[PHASE 3] Database Maintenance")
        deleted_count = database.cleanup_old_records(ROW_RETENTION_DAYS)
        if deleted_count > 0:
            print(f"[CLEANUP] Removed {deleted_count} records older than {ROW_RETENTION_DAYS} days.")

        print("\n[PHASE 4] Refreshing public log views")
        flush_views()

        print("\n[PHASE 5] Health summary")
        health_summary(HEALTH_STATE)
        try:
            dispatch_alerts(HEALTH_STATE, notify_owner, dry_run=DRY_RUN)
        except Exception as e:
            print(f"[ERROR] dispatch_alerts raised: {e}")
            traceback.print_exc()
        save_health(HEALTH_STATE)

        print("\n[SUCCESS] Bot execution completed successfully!")

    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Bot execution cancelled by user.")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Bot execution failed: {e}")
        traceback.print_exc()
        # Try to persist whatever health info we have, even on partial failure
        if HEALTH_STATE is not None:
            try:
                save_health(HEALTH_STATE)
            except Exception:
                pass

if __name__ == "__main__":
    main()
