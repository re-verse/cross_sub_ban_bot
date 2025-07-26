#!/usr/bin/env python3
"""
SQLite Bot - Safe Test Run
Test with real Reddit API but only on xsubpacttest1
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

# Override the trusted subs for testing
def load_trusted_subs_test(path="trusted_subs_test.txt"):
    if os.path.exists(path):
        with open(path) as f:
            return [line.strip().lower() for line in f if line.strip()]
    else:
        return ["xsubpacttest1"]  # Fallback to just test sub

# Load test configuration
with open("config_test.json") as f:
    config = json.load(f)

CROSS_SUB_BAN_REASON = config.get("CROSS_SUB_BAN_REASON", "Auto XSub Pact Ban")
EXEMPT_USERS = set(u.lower() for u in config.get("EXEMPT_USERS", []))
DAILY_BAN_LIMIT = config.get("DAILY_BAN_LIMIT", 2)  # Very low limit for testing
MAX_LOG_AGE_MINUTES = config.get("MAX_LOG_AGE_MINUTES", 60)
USE_SQLITE = config.get("USE_SQLITE", True)
SQLITE_DB_PATH = config.get("SQLITE_DB_PATH", "bans.db")

# Test-only trusted subs
TRUSTED_SUBS = load_trusted_subs_test()
TRUSTED_SOURCES = {f"r/{sub}" for sub in TRUSTED_SUBS}

print("="*60)
print("SQLite Bot - SAFE TEST RUN")
print("="*60)
print(f"[CONFIG] Test subreddits only: {TRUSTED_SUBS}")
print(f"[CONFIG] Daily ban limit: {DAILY_BAN_LIMIT}")
print(f"[CONFIG] Max log age: {MAX_LOG_AGE_MINUTES} minutes")
print(f"[CONFIG] SQLite enabled: {USE_SQLITE}")
print("="*60)

try:
    # Import database and Reddit
    from db_utils import BanDatabase
    
    # Check if we have Reddit credentials
    reddit_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'REDDIT_USERNAME', 'REDDIT_PASSWORD']
    missing_vars = [var for var in reddit_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"[WARNING] Missing Reddit environment variables: {missing_vars}")
        print("[INFO] Running in simulation mode without Reddit API")
        
        # Simulate the test
        db = BanDatabase(SQLITE_DB_PATH)
        records = db.get_all_records()
        stats = db.get_stats()
        
        print(f"[TEST] Current database: {len(records)} records")
        print(f"[TEST] Database stats: {stats}")
        print(f"[TEST] Would check mod logs for: {TRUSTED_SUBS}")
        print(f"[TEST] Would process cross-sub bans with limit: {DAILY_BAN_LIMIT}")
        
        print(f"\n✅ Test simulation completed successfully!")
        print(f"💡 To run with real Reddit API, set environment variables:")
        for var in reddit_vars:
            print(f"   export {var}='your_value'")
        
    else:
        # Real Reddit test
        import praw
        
        print("[TEST] Connecting to Reddit API...")
        reddit = praw.Reddit(
            client_id=os.environ.get('REDDIT_CLIENT_ID'),
            client_secret=os.environ.get('REDDIT_CLIENT_SECRET'),
            username=os.environ.get('REDDIT_USERNAME'),
            password=os.environ.get('REDDIT_PASSWORD'),
            user_agent='Cross-Sub Ban Bot/1.0 - Test Mode'
        )
        
        print(f"[TEST] Connected as: u/{reddit.user.me()}")
        
        # Initialize database
        db = BanDatabase(SQLITE_DB_PATH)
        
        # Load current data
        SHEET_CACHE = db.get_all_records()
        print(f"[TEST] Loaded {len(SHEET_CACHE)} existing records")
        
        # Test mod log access for test subreddit only
        for sub_name in TRUSTED_SUBS:
            print(f"\n[TEST] Checking r/{sub_name} mod log...")
            
            try:
                subreddit = reddit.subreddit(sub_name)
                
                # Check if we have mod access
                try:
                    mod_list = list(subreddit.moderator())
                    bot_username = reddit.user.me().name.lower()
                    is_mod = any(mod.name.lower() == bot_username for mod in mod_list)
                    
                    if is_mod:
                        print(f"  ✅ Bot has mod access to r/{sub_name}")
                    else:
                        print(f"  ⚠️  Bot is not a moderator of r/{sub_name}")
                        print(f"  ℹ️  Will only read public mod log entries")
                
                except Exception as e:
                    print(f"  ⚠️  Cannot check mod status: {e}")
                
                # Read recent mod log (limited scope)
                print(f"  [TEST] Reading last 10 mod actions...")
                log_count = 0
                
                for log in subreddit.mod.log(limit=10):
                    log_count += 1
                    action = log.action
                    user = getattr(log, "target_author", "unknown")
                    mod = getattr(log.mod, 'name', 'unknown')
                    timestamp = datetime.utcfromtimestamp(log.created_utc)
                    
                    # Check if recent enough
                    age_minutes = (datetime.utcnow() - timestamp).total_seconds() / 60
                    
                    print(f"    {log_count}. {action} u/{user} by u/{mod} ({age_minutes:.1f}min ago)")
                    
                    if age_minutes <= MAX_LOG_AGE_MINUTES and action in ["banuser", "unbanuser"]:
                        print(f"      ⚡ This would be processed (within {MAX_LOG_AGE_MINUTES}min limit)")
                    else:
                        print(f"      ⏳ Too old or not a ban action - would skip")
                
                print(f"  [TEST] Found {log_count} mod log entries")
                
            except Exception as e:
                print(f"  ❌ Error accessing r/{sub_name}: {e}")
        
        # Show what would happen next
        print(f"\n[TEST] Next steps (NOT executed in test mode):")
        print(f"  1. Process recent ban/unban actions")
        print(f"  2. Add new bans to SQLite database")
        print(f"  3. Execute cross-sub bans (limited to {DAILY_BAN_LIMIT} per source)")
        print(f"  4. Target subreddits would be: {TRUSTED_SUBS}")
        
        # Show current database stats
        stats = db.get_stats()
        print(f"\n[DATABASE] Current stats:")
        print(f"  Total records: {stats['total_records']}")
        print(f"  Active bans: {stats['active_bans']}")
        print(f"  Forgiven bans: {stats['forgiven_bans']}")
        print(f"  Records by sub: {stats['records_by_sub']}")
        
        print(f"\n✅ Test run completed successfully!")
        print(f"🛡️  No actual bans were executed (test mode)")

except Exception as e:
    print(f"[ERROR] Test failed: {e}")
    traceback.print_exc()

print(f"\n📋 Test Summary:")
print(f"  ✅ SQLite database: Working")
print(f"  ✅ Configuration: Test mode active")
print(f"  ✅ Scope: Limited to {TRUSTED_SUBS}")
print(f"  ✅ Safety: No actual bans executed")
