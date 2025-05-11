#!/usr/bin/env python3

import json
import base64
import os
import sys
import time
import praw
import prawcore
import gspread
import traceback
import re
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from core_utils import (
    is_mod,
    is_forgiven,
    exempt_subs_for_user,
)
from log_utils import log_public_action, flush_public_markdown_log
from modmail_utils import check_modmail, apply_override, apply_exemption
from stats_utils import write_stats_sheet
from bot_config import (
    WORK_DIR,
    PUBLIC_LOG_JSON,
    PUBLIC_LOG_MD,
    CROSS_SUB_BAN_REASON,
    EXEMPT_USERS,
    DAILY_BAN_LIMIT,
    MAX_LOG_AGE_MINUTES,
    ROW_RETENTION_DAYS,
    TRUSTED_SUBS,
    TRUSTED_SOURCES,
    sheet,
    client,
    reddit,
    sheet_key
)



# --- Caches ---
mod_cache = {}
SHEET_CACHE = []

# --- Helper Functions ---

def load_sheet_cache():
    global SHEET_CACHE
    try:
        start = time.time()
        SHEET_CACHE = sheet.get_all_records()
        print(f"[DEBUG] Sheet load took {time.time() - start:.2f}s")
        print(f"[INFO] Loaded {len(SHEET_CACHE)} rows into local cache.")
    except Exception as e:
        print(f"[ERROR] Failed to load sheet cache: {e}")
        SHEET_CACHE = []

# --- Ban Sync ---

def sync_bans_from_sub(sub):
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
                if log.action in ("banuser", "unbanuser"):
                    print(f"[WARN] Skipping log {log_id} - No valid target user found")
                continue

            if datetime.utcnow() - ts > timedelta(minutes=MAX_LOG_AGE_MINUTES):
                continue

            user_lc = user.strip().lower()

            # --- Handle UNBAN actions as forgiveness ---
            if action == "unbanuser":
                match = [
                    (i, row)
                    for i, row in enumerate(SHEET_CACHE, start=2)
                    if row.get("Username", "").strip().lower() == user_lc
                    and row.get("SourceSub", "").strip().lower() == source
                    and not row.get("ForgiveTimestamp", "").strip()
                ]
                if match:
                    row_num, row = match[0]
                    forgive_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[FORGIVE] u/{user} unbanned in {source} by {mod} – marking as forgiven.")
                    try:
                        sheet.update_cell(row_num, 5, "yes")  # ManualOverride
                        sheet.update_cell(row_num, 7, mod)    # OverriddenBy
                        sheet.update_cell(row_num, 8, sub)    # ModSub
                        sheet.update_cell(row_num, 9, forgive_time)  # ForgiveTimestamp
                        SHEET_CACHE[row_num - 2]["ManualOverride"] = "yes"
                        SHEET_CACHE[row_num - 2]["OverriddenBy"] = mod
                        SHEET_CACHE[row_num - 2]["ModSub"] = sub
                        SHEET_CACHE[row_num - 2]["ForgiveTimestamp"] = forgive_time
                    except Exception as e:
                        print(f"[ERROR] Failed to update forgiveness for u/{user}: {e}")
                    continue

            # --- Handle BAN actions ---
            if action != "banuser":
                continue

            if CROSS_SUB_BAN_REASON.lower() not in desc.lower() and desc.lower() != "auto xsub pact ban":
                print(f"[WARN] Skipping log {log_id} for {user}: Description doesn't match ('{desc}')")
                continue

            if source not in TRUSTED_SOURCES:
                print(f"[DEBUG] SKIP {log_id} for {user!r}: source {source!r} not trusted")
                continue

            if user_lc in EXEMPT_USERS or is_mod(sr, user):
                continue

            if user_lc in seen_user_sources or any(
                r.get('Username', '').strip().lower() == user_lc for r in SHEET_CACHE
            ):
                print(f"[SKIP] Already logged user {user_lc} to sheet (from any sub)")
                continue
            seen_user_sources.add(user_lc)

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
                print("[DEBUG] About to append row:", row_data)
                sheet.append_row(row_data, value_input_option='USER_ENTERED')
            except Exception as e:
                print(f"[ERROR] FAILED to log user '{user}' to sheet for r/{sub}: {e}")
                traceback.print_exc()
                continue

            SHEET_CACHE.append({
                'Username': user,
                'SourceSub': source,
                'Reason': CROSS_SUB_BAN_REASON,
                'Timestamp': ts.strftime('%Y-%m-%d %H:%M:%S'),
                'ManualOverride': '',
                'ModLogID': log_id,
                'Mod': mod,
                'ModSub': '',
                'ForgiveTimestamp': '',
                'ExemptSubs': ''
            })

            print(f"[LOGGED] {user} banned in {source} by {mod}")

    except (prawcore.exceptions.Forbidden, prawcore.exceptions.NotFound):
        print(f"[WARN] Cannot access modlog for r/{sub}, skipping.")

# --- Ban Enforcer ---
def enforce_bans_on_sub(sub):
    print(f"[STEP] Enforcing bans/unbans in r/{sub}")
    action_was_taken_by_queue = False

    try:
        sr = reddit.subreddit(sub)
        FETCH_LIMIT = 100
        print(f"[INFO] Fetching the latest {FETCH_LIMIT} bans for r/{sub}...")
        bans = {b.name.lower(): b for b in sr.banned(limit=FETCH_LIMIT)}
        print(f"[INFO] Fetched {len(bans)} bans.")
    except prawcore.exceptions.TooManyRequests:
        print(f"[WARN] Hit rate limit fetching ban list for r/{sub}. Skipping enforcement for this sub.")
        return
    except Exception as e:
        print(f"[ERROR] Cannot fetch ban list for r/{sub} ({type(e).__name__}): {e}")
        import traceback
        traceback.print_exc()
        return

    all_rows = SHEET_CACHE
    now = datetime.utcnow()
    cutoff = now - timedelta(days=1)

    actions_to_take = []
    seen = set()

    print(f"[INFO] Checking {len(all_rows)} sheet entries against r/{sub} ban list...")
    for r in all_rows:
        user = r.get('Username', '')
        src = r.get('SourceSub', '')
        ts_str = r.get('Timestamp', '')
        if not user or not src or not ts_str:
            continue
        try:
            entry_time = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        except:
            continue
        if entry_time < cutoff:
            continue  # Skip old rows

        key = (user.lower(), src.lower())
        if key in seen:
            continue
        seen.add(key)

        ul = user.lower()
        deleted_marker = str(r.get('ForgiveTimestamp', '')).strip()
        if deleted_marker:
            continue

        # --- Check for UNBAN actions ---
        should_unban = False
        unban_reason = ""
        if is_forgiven(user, SHEET_CACHE):
            should_unban = True
            unban_reason = "Forgiven override"
        elif sub.lower() in exempt_subs_for_user(user, SHEET_CACHE):
            should_unban = True
            unban_reason = "Per-sub exemption override"

        if should_unban:
            if ul in bans and CROSS_SUB_BAN_REASON.lower() in (getattr(bans.get(ul), 'note', '') or '').lower():
                actions_to_take.append(('unban', user, src, unban_reason))
            continue

        # --- Check for BAN actions ---
        if ul in EXEMPT_USERS or is_mod(sr, user):
            continue

        if ul in bans:
            existing_note = getattr(bans[ul], 'note', '') or ''
            if CROSS_SUB_BAN_REASON.lower() in existing_note.lower():
                print(f"[SKIP] u/{user} already banned in r/{sub} with correct reason.")
                continue

        actions_to_take.append(('ban', user, src, ""))

    print(f"[INFO] Processing {len(actions_to_take)} queued actions for r/{sub}...")
    for action_type, username, source_sub, reason_note in actions_to_take:
        try:
            if action_type == 'unban':
                sr.banned.remove(username)
                print(f"[UNBANNED] (Queued) u/{username} in r/{sub} ({reason_note})")
                log_public_action("UNBANNED", username, sub, source_sub, "Bot (Queued)", reason_note)
                action_was_taken_by_queue = True
            elif action_type == 'ban':
                ban_note = (
                    f"Cross-sub ban from {source_sub}. NHL subs share a pact to fight trolling. "
                    f"To appeal, message mods of {source_sub}, admit what you did, and promise to follow rules. "
                    f"If they forgive, a global unban will follow."

                )
                sr.banned.add(username, ban_reason=CROSS_SUB_BAN_REASON, note=ban_note)
                print(f"[BANNED] (Queued) u/{username} in r/{sub} from {source_sub}")
                log_public_action("BANNED", username, sub, source_sub, "Bot (Queued)", "")
                action_was_taken_by_queue = True
            time.sleep(2)

        except prawcore.exceptions.TooManyRequests:
            print(f"[WARN] Hit rate limit during queued action for u/{username} in r/{sub}. Sleeping longer...")
            time.sleep(30)
        except praw.exceptions.RedditAPIException as e:
            print(f"[ERROR] Queued action API Error for u/{username} in r/{sub}: {e}")
            for subexc in e.items:
                if subexc.error_type == 'USER_DOESNT_EXIST':
                    print(f"[INFO] Skipping action for non-existent user u/{username}.")
                    break
                elif subexc.error_type == 'SUBREDDIT_BAN_NOT_PERMITTED':
                    print(f"[WARN] Bot lacks permission to ban u/{username} in r/{sub}.")
                    break
                elif subexc.error_type == 'USER_ALREADY_BANNED':
                    print(f"[INFO] Skipping ban, u/{username} already banned in r/{sub}.")
                    break
        except Exception as e:
            print(f"[ERROR] Unexpected error during queued action for u/{username} in r/{sub} ({type(e).__name__}): {e}")
            import traceback
            traceback.print_exc()

    if not action_was_taken_by_queue:
        print(f"[INFO] No bans or unbans needed/performed via queue in r/{sub}.")
        
# --- Main ---

if __name__ == '__main__':
    print("=== Running Cross-Sub Ban Bot ===")

    print("[INFO] Loading sheet cache...")
    load_sheet_cache()
    print("[INFO] Sheet cache loaded.")
    print("[INFO] Checking modmail threads...")
    check_modmail() # Modmail check already loops internally
    print("[INFO] Modmail check complete.")
    
    print("[INFO] Starting ban sync phase...")
    for s in TRUSTED_SUBS:
        print(f"\n=== [SYNC] Processing r/{s} ===")
        load_sheet_cache()
        sync_bans_from_sub(s)
        # --- DELAY 1 ---
        print(f"[INFO] Pausing briefly after checking r/{s} modlog...")
        time.sleep(2)  # Pause for 2 seconds 
        
    print("[INFO] Sync phase complete. Pausing before enforcement phase...")
    time.sleep(15) 

    print("[INFO] Starting ban enforcement phase...")
    
    for s in TRUSTED_SUBS:
        enforce_bans_on_sub(s)
        # --- DELAY 2 ---
        print(f"[INFO] Pausing after enforcing bans in r/{s}...")
        time.sleep(3) # Pause for 3 seconds (maybe slightly longer)
        
    print("[INFO] Enforcement phase complete.")
    
    flush_public_markdown_log()
    
    print("=== Bot run complete ===")
    
    write_stats_sheet(SHEET_CACHE, client, sheet_key)
    sys.exit(0)
