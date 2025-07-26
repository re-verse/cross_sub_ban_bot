#!/usr/bin/env python3
"""
Quick local test of SQLite migration
Run this to verify the bot works before pushing to GitHub
"""

import os
import sys
import time

print("🔍 SQLite Migration Local Test")
print("=" * 50)

# Check if we're in the right directory
if not os.path.exists('cross_sub_ban_bot_sqlite.py'):
    print("❌ Error: Run this from the cross_sub_ban_bot directory")
    sys.exit(1)

print("✅ Found SQLite bot files")

# Check for requirements
try:
    import praw
    print("✅ PRAW installed")
except ImportError:
    print("❌ PRAW not installed - run: pip install -r requirements_sqlite.txt")
    sys.exit(1)

# Check database
if os.path.exists('bans.db'):
    print("✅ Database exists")
    from db_utils import BanDatabase
    db = BanDatabase('bans.db')
    records = db.get_all_records()
    print(f"📊 Found {len(records)} ban records")
else:
    print("⚠️  No database found - will be created on first run")

# Run safe test
print("\n🧪 Running safe test (no Reddit API)...")
try:
    import subprocess
    result = subprocess.run([sys.executable, 'test_bot_safe.py'], 
                          capture_output=True, text=True)
    if result.returncode == 0:
        print("✅ Safe test PASSED!")
        print(result.stdout)
    else:
        print("❌ Safe test FAILED!")
        print(result.stderr)
except Exception as e:
    print(f"❌ Error running safe test: {e}")

print("\n" + "=" * 50)
print("📋 Summary:")
print("- SQLite bot is ready for testing")
print("- Safe test validates database operations")
print("- To run Reddit API test: python test_bot_real.py")
print("- GitHub Actions workflow is now available")
