import os
import json
import base64
import praw
from oauth2client.service_account import ServiceAccountCredentials

# --- Directory and log paths ---
WORK_DIR = "/home/runner/work/cross_sub_ban_bot/cross_sub_ban_bot"
PUBLIC_LOG_JSON = f"{WORK_DIR}/public_ban_log.json"
PUBLIC_LOG_MD = f"{WORK_DIR}/public_ban_log.md"

# --- Load config.json ---
with open("config.json") as f:
    config = json.load(f)


CROSS_SUB_BAN_REASON   = config.get("CROSS_SUB_BAN_REASON", "Auto XSub Pact Ban")
EXEMPT_USERS           = set(u.lower() for u in config.get("EXEMPT_USERS", []))
DAILY_BAN_LIMIT        = config.get("DAILY_BAN_LIMIT", 50)
MAX_LOG_AGE_MINUTES    = config.get("MAX_LOG_AGE_MINUTES", 600)
ROW_RETENTION_DAYS     = config.get("ROW_RETENTION_DAYS", 10)

# --- Load trusted subs from file ---
def load_trusted_subs(path="trusted_subs.txt"):
    with open(path) as f:
        return [line.strip().lower() for line in f if line.strip()]

TRUSTED_SUBS    = load_trusted_subs()
TRUSTED_SOURCES = {f"r/{sub}" for sub in TRUSTED_SUBS}

# --- Reddit API setup ---
def setup_reddit():
    return praw.Reddit(
        client_id=os.environ.get('REDDIT_CLIENT_ID') or os.environ.get('CLIENT_ID'),
        client_secret=os.environ.get('REDDIT_CLIENT_SECRET') or os.environ.get('CLIENT_SECRET'),
        username=os.environ.get('REDDIT_USERNAME') or os.environ.get('USERNAME'),
        password=os.environ.get('REDDIT_PASSWORD') or os.environ.get('PASSWORD'),
        user_agent='Cross-Sub Ban Bot/1.0'
    )

# --- Instantiate API clients here ---
sheet, client, sheet_key = setup_google_sheet()
reddit = setup_reddit()
