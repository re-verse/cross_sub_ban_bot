#!/usr/bin/env python3

WORK_DIR = "/home/runner/work/cross_sub_ban_bot/cross_sub_ban_bot"

import json
import base64
import os
import sys
import praw
import prawcore
import gspread
import traceback
import re
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

# --- Counters ---
ban_counter = 0
unban_counter = 0

from bot_config import (
    CROSS_SUB_BAN_REASON,
    EXEMPT_USERS,
    DAILY_BAN_LIMIT,
    MAX_LOG_AGE_MINUTES,
    ROW_RETENTION_DAYS,
    TRUSTED_SUBS,
    TRUSTED_SOURCES,
    sheet,
    client,
    reddit
)

# --- Caches ---
mod_cache = {}
SHEET_CACHE = []

# --- Helper Functions ---
def is_mod(subreddit, user):
    sub = subreddit.display_name.lower()
    if sub not in mod_cache:
        try:
            mod_cache[sub] = {m.name.lower() for m in subreddit.moderator()}
        except Exception:
            mod_cache[sub] = set()
    return user.lower() in mod_cache[sub]

def exempt_subs_for_user(user):
    for r in SHEET_CACHE:
        if r.get('Username','').lower() == user.lower():
            field = str(r.get('ExemptSubs','')).lower()
            if field:
                return {sub.strip() for sub in field.split(',') if sub.strip()}
    return set()

def apply_exemption(username, modsub):
    records = sheet.get_all_records()
    for i, r in enumerate(records, start=2):
        if r.get('Username','').lower() == username.lower():
            current = str(r.get('ExemptSubs','')).lower()
            parts = {p.strip() for p in current.split(',') if p.strip()}
            parts.add(modsub.lower())
            new_field = ', '.join(sorted(parts))
            sheet.update_cell(i, 10, new_field)
            return True
    return False

def is_forgiven(user):
    for r in SHEET_CACHE:
        if r.get('Username','').lower() == user.lower() and str(r.get('ManualOverride','')).lower() in ('yes','true'):
            return True
    return False

def already_logged_action(log_id):
    if not log_id:
        return False
    log_id = str(log_id).strip().lower()
    for row in SHEET_CACHE:
        if str(row.get('ModLogID', '')).strip().lower() == log_id:
            return True
    return False


def get_recent_sheet_entries(source_sub):
    cutoff = datetime.utcnow() - timedelta(days=1)
    count = 0
    for r in SHEET_CACHE:
        if r.get('SourceSub') == source_sub:
            ts = r.get('Timestamp')
            try:
                t = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
                if t > cutoff:
                    count += 1
            except:
                pass
    return count

def log_public_action(action, username, subreddit, source_sub="", actor="", note=""):
    entry = {
        "timestamp": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        "action": action,
        "username": username,
        "subreddit": subreddit,
        "source_sub": source_sub,
        "actor": actor,
        "note": note
    }
    try:
        data = []
        if os.path.exists(PUBLIC_LOG_JSON):
            try:
                with open(PUBLIC_LOG_JSON, 'r') as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                print(f"[WARN] {PUBLIC_LOG_JSON} exists but is invalid. Starting fresh.")
                data = []

        data.append(entry)
        with open(PUBLIC_LOG_JSON, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"[INFO] Logged public action: {entry}")

    except Exception as e:
        print(f"[ERROR] Failed to write to public ban log JSON: {e}")


def load_sheet_cache():
    global SHEET_CACHE
    try:
        SHEET_CACHE = sheet.get_all_records()
        print(f"[INFO] Loaded {len(SHEET_CACHE)} rows into local cache.")
        for row in SHEET_CACHE:
            print("[DEBUG] CACHE ROW:", row)
    except Exception as e:
        print(f"[ERROR] Failed to load sheet cache: {e}")
        SHEET_CACHE = []

# --- Modmail Checking ---
def check_modmail():
    print("[STEP] Checking for pardon and exemption messages...")
    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
            for state in ("new", "mod",):
                for convo in sr.modmail.conversations(state=state):
                    if not convo.messages:
                        continue
                    last = convo.messages[-1]
                    body = getattr(last, 'body_markdown', '').strip()
                    sender = getattr(last.author, 'name', '').lower()
                    if not sender or not body:
                        continue
                    if not is_mod(sr, sender):
                        continue
                    if body.lower().startswith('/xsub pardon'):
                        parts = body.split()
                        if len(parts) >= 3:
                            user = parts[2].lstrip('u/').strip()
                            apply_override(user, sender, sub)
                            convo.reply(body=f"✅ u/{user} has been forgiven and will not be banned.")
                    elif body.lower().startswith('/xsub exempt'):
                        parts = body.split()
                        if len(parts) >= 3:
                            user = parts[2].lstrip('u/').strip()
                            if apply_exemption(user, sub):
                                convo.reply(body=f"✅ u/{user} has been exempted from bans in r/{sub}.")
        except Exception:
            continue

def apply_override(username, moderator, modsub):
    records = sheet.get_all_records()
    for i,r in enumerate(records, start=2):
        if r.get('Username','').lower() == username.lower():
            sheet.update_cell(i,5,'yes')
            sheet.update_cell(i,7,moderator)
            sheet.update_cell(i,8,modsub)
            return True
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    sheet.append_row([username,'manual','',now,'yes','',moderator,modsub,''])
    return True

# --- Ban Sync ---

def sync_bans_from_sub(sub):
    print(f"[STEP] Checking modlog for r/{sub}")
    try:
        sr = reddit.subreddit(sub)

        for log in sr.mod.log(action='banuser', limit=200):
            print(f"[TRACE] log_id={log.id}, action={log.action}, user={getattr(log, 'target_author', None)}")
            print(f"[DEBUG] MODLOG ENTRY FOR {sub}:")
            print(f"  log.id = {log.id}")
            print(f"  log.mod = {log.mod} (type: {type(log.mod)})")
            print(f"  getattr(log.mod, 'name', None) = {getattr(log.mod, 'name', None)}")
            print(f"  log.target_author = {getattr(log, 'target_author', None)}")
            print(f"  log.description = {getattr(log, 'description', None)}")
            log_id = log.id
            mod = getattr(log.mod, 'name', 'unknown')
            desc = (log.description or '').strip()
            source = f"r/{log.subreddit}".lower()

            # debug drop if not in trusted
            if source not in TRUSTED_SOURCES:
                print(f"[DEBUG] SKIP {log_id}: source {source!r} not in TRUSTED_SOURCES {TRUSTED_SOURCES}")
                continue
            ts     = datetime.utcfromtimestamp(log.created_utc)

            # Simplified username resolution
            user = getattr(log, "target_author", None)
            if not isinstance(user, str) or not user.strip():
                user = "[unknown_user]"

            print(f"[DEBUG] Writing modlog dump for {sub} to {os.path.join(WORK_DIR, f'modlog_dump_{sub}.txt')}")
            with open(os.path.join(WORK_DIR, f"modlog_dump_{sub}.txt"), "a") as f:
                f.write(f"{datetime.utcnow().isoformat()} | log_id={log_id} | user={user} | mod={mod} | desc={desc}\n")

            print(f"[DEBUG] log_id={log_id}, mod={mod}, target_author={user}, desc='{desc}'")

            if user == "[unknown_user]":
                print(f"[WARN] Skipping log {log_id} - No valid target user found (user={user})")
                continue

            if datetime.utcnow() - ts > timedelta(minutes=MAX_LOG_AGE_MINUTES):
                continue

            if CROSS_SUB_BAN_REASON.lower() not in desc.lower():
                print(f"[WARN] Skipping log {log_id} for {user}: Description doesn't contain expected reason ('{desc}')")
                continue

            if source not in TRUSTED_SOURCES:
                print(f"[DEBUG] SKIP {log_id} for {user!r}: source {source!r} not trusted")
                continue

            if user.lower() in EXEMPT_USERS or is_mod(sr, user):
                continue

            if already_logged_action(log_id):
                continue

            # Skip if (user, source) combo is already in the sheet
            if any(r.get('Username', '').lower() == user.lower() and r.get('SourceSub', '').lower() == source for r in SHEET_CACHE):
                print(f"[SKIP] Already logged ({user}, {source}) to sheet")
                continue
                
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
                print("[DEBUG] APPEND SUCCESS")
            except Exception as e:
                print(f"[ERROR] FAILED to log user '{user}' to sheet for r/{sub}")
                print("[CRITICAL] Row data that caused failure:", row_data)
                print(f"Error Type: {type(e).__name__}, Message: {e}")
                traceback.print_exc()
                raise
            else:
                print("[DEBUG] Append completed without triggering exception block")
                print("[DEBUG] APPEND SUCCESS", flush=True)

                

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
    global ban_counter, unban_counter

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
        if is_forgiven(user):
            should_unban = True
            unban_reason = "Forgiven override"
        elif sub.lower() in exempt_subs_for_user(user):
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
                unban_counter += 1
                action_was_taken_by_queue = True
            elif action_type == 'ban':
                sr.banned.add(username, ban_reason=CROSS_SUB_BAN_REASON, note=f"Cross-sub ban from {source_sub}")
                print(f"[BANNED] (Queued) u/{username} in r/{sub} from {source_sub}")
                log_public_action("BANNED", username, sub, source_sub, "Bot (Queued)", "")
                ban_counter += 1
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
        
# --- End of function ---

# --- Public Markdown Log Writer ---
def flush_public_markdown_log():
    try:
        if os.path.exists(PUBLIC_LOG_JSON):
            with open(PUBLIC_LOG_JSON, 'r') as f:
                entries = json.load(f)
        else:
            entries = []

        with open(PUBLIC_LOG_MD, 'w') as f:
            f.write("# NHL Cross-Sub Ban Log\n\n")
            f.write("This file is auto-generated by the bot.\n\n")
            f.write(f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n")
            f.write("---\n\n")
            for entry in entries:
                f.write(f"### [{entry['timestamp']}] {'✅' if entry['action']=='UNBANNED' else '❌'} {entry['action']} u/{entry['username']}\n")
                f.write(f"- **Subreddit**: r/{entry['subreddit']}\n")
                if entry.get('source_sub'):
                    f.write(f"- **Source Sub**: {entry['source_sub']}\n")
                if entry.get('actor'):
                    f.write(f"- **Actor**: {entry['actor']}\n")
                if entry.get('note'):
                    f.write(f"- **Note**: {entry['note']}\n")
                f.write("\n")
    except Exception as e:
        print(f"[ERROR] Failed to flush public ban markdown log: {e}")

# --- Google Sheets Stats ---

def write_stats_sheet():
    try:
        stats_sheet = client.open_by_key(sheet_key).worksheet("Stats")
    except gspread.exceptions.WorksheetNotFound:
        stats_sheet = client.open_by_key(sheet_key).add_worksheet(title="Stats", rows="100", cols="10")

    # Tally from sheet cache
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)
    daily_counts = {}
    weekly_counts = {}
    user_counts = {}

    for row in SHEET_CACHE:
        ts_str = row.get("Timestamp", "")
        src = row.get("SourceSub", "unknown")
        actor = row.get("Mod", "unknown")
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except:
            continue

        date_key = ts.date().isoformat()
        src = src.strip() or "unknown"
        actor = actor.strip() or "unknown"

        daily_counts.setdefault(date_key, {}).setdefault(src, 0)
        daily_counts[date_key][src] += 1

        if ts.date() >= week_ago:
            weekly_counts.setdefault(src, 0)
            weekly_counts[src] += 1

        user_counts.setdefault(actor, 0)
        user_counts[actor] += 1

    # Overwrite entire Stats sheet
    stats_sheet.clear()
    stats_sheet.update(values=[["📅 Daily Ban Count"]], range_name="A1")
    row = 2
    for day in sorted(daily_counts.keys(), reverse=True):
        for sub, count in daily_counts[day].items():
            stats_sheet.update(range_name=f"A{row}", values=[[day, sub, count]])
            row += 1

    row += 1
    stats_sheet.update(range_name=f"A{row}", values=[["📈 Weekly Bans Per Subreddit"]])
    row += 1
    for sub, count in sorted(weekly_counts.items(), key=lambda x: -x[1]):
        stats_sheet.update(range_name=f"A{row}", values=[[sub, count]])

        row += 1

    row += 1
    stats_sheet.update(range_name=f"A{row}", values=[["🏆 Top Banning Moderators"]])
    row += 1
    for mod, count in sorted(user_counts.items(), key=lambda x: -x[1]):
        stats_sheet.update(range_name=f"A{row}", values=[[mod, count]])
        row += 1

    print("[INFO] Stats written to 'Stats' worksheet.")

# --- Main ---
import time  

if __name__ == '__main__':
    print("=== Running Cross-Sub Ban Bot ===")
    
    load_sheet_cache()
    check_modmail() # Modmail check already loops internally
    
    print("[INFO] Starting ban sync phase...")
    for s in TRUSTED_SUBS:
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
    
    print(f"=== Summary ===")
    print("================")
    print("=== Bot run complete ===")
    
    write_stats_sheet()
    sys.exit(0)
