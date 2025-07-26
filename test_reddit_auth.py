#!/usr/bin/env python3
"""
Test Reddit API authentication for the SQLite bot
This will help diagnose the 401 error
"""

import os
import sys

print("🔍 Reddit API Authentication Test")
print("=" * 50)

# Check environment
try:
    import praw
    print("✅ PRAW installed")
except ImportError:
    print("❌ PRAW not installed")
    print("Run: source migration_env/bin/activate && pip install praw")
    sys.exit(1)

# Check for credentials
creds = {
    'REDDIT_CLIENT_ID': os.getenv('REDDIT_CLIENT_ID', 'Not set'),
    'REDDIT_CLIENT_SECRET': os.getenv('REDDIT_CLIENT_SECRET', 'Not set')[:10] + '...' if os.getenv('REDDIT_CLIENT_SECRET') else 'Not set',
    'REDDIT_USERNAME': os.getenv('REDDIT_USERNAME', 'Not set'),
    'REDDIT_PASSWORD': os.getenv('REDDIT_PASSWORD', 'Set' if os.getenv('REDDIT_PASSWORD') else 'Not set')
}

print("\n📋 Credentials check:")
for key, value in creds.items():
    print(f"  {key}: {value}")

if 'Not set' in creds.values():
    print("\n❌ Missing credentials. Set them like this:")
    print("export REDDIT_CLIENT_ID='your_client_id'")
    print("export REDDIT_CLIENT_SECRET='your_secret'")
    print("export REDDIT_USERNAME='xsub-pact-bot'")
    print("export REDDIT_PASSWORD='your_password'")
    sys.exit(1)

# Try to authenticate
print("\n🔐 Attempting Reddit authentication...")
try:
    reddit = praw.Reddit(
        client_id=os.getenv('REDDIT_CLIENT_ID'),
        client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
        username=os.getenv('REDDIT_USERNAME'),
        password=os.getenv('REDDIT_PASSWORD'),
        user_agent='xsub-pact-bot/1.0'
    )
    
    # Test the connection
    me = reddit.user.me()
    print(f"✅ Authentication successful! Logged in as: {me.name}")
    print(f"📊 Comment karma: {me.comment_karma}")
    print(f"📊 Link karma: {me.link_karma}")
    
except Exception as e:
    print(f"❌ Authentication failed: {e}")
    print("\nPossible issues:")
    print("1. Password may have changed")
    print("2. 2FA might be enabled")
    print("3. App permissions insufficient")
    print("4. Credentials incorrect")
