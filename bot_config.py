import os
import json
import base64
import gspread
import praw
from oauth2client.service_account import ServiceAccountCredentials

# --- Load configuration file ---
with open("config.json") as f:
    config = json.load(f)

CROSS_SUB_BAN_REASON = config.get("CROSS_SUB_BAN_REASON", "Auto XSub Pact Ban")
EXEMPT_USERS = set(u.lower() for u in config.get("EXEMPT_USERS", []))
DAILY_BAN_LIMIT = config.get("DAILY_BAN_LIMIT", 50)
MAX_LOG_AGE_MINUTES = config.get("MAX_LOG_AGE_MINUTES", 45)
ROW_RETENTION_DAYS = config.get("ROW_RETENTION_DAYS", 10)

# --- Load trusted subs ---
def load_trusted_subs(path="trusted_subs.txt"):
    with open(path) as f:
        return [line.strip().lower() for line in f if line.strip()]

TRUSTED_SUBS = load_trusted_subs()
TRUSTED_SOURCES = {f"r/{sub}" for sub in TRUSTED_SUBS}

# --- Google Sheets setup ---
def setup_google_sheet():
    creds_env = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not creds_env:
        raise SystemExit("[FATAL] Missing GOOGLE_SERVICE_ACCOUNT_JSON env var.")
    try:
        decoded = base64.b64decode(creds_env)
        creds_str = decoded.decode('utf-8')
        creds_dict = json.loads(creds_str)
    except Exception:
        creds_dict = json.loads(creds_env)

    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)

    sheet_key = os.environ.get('GOOGLE_SHEET_ID')
    if not sheet_key:
        raise SystemExit("[FATAL] Missing GOOGLE_SHEET_ID env var.")
    sheet = client.open_by_key(sheet_key).sheet1
    print(f"[INFO] Google Sheet '{sheet_key}' opened, worksheet '{sheet.title}' loaded.")
    return sheet, client

# --- Reddit setup ---
def setup_reddit():
    return praw.Reddit(
        client_id=os.environ.get('REDDIT_CLIENT_ID') or os.environ.get('CLIENT_ID'),
        client_secret=os.environ.get('REDDIT_CLIENT_SECRET') or os.environ.get('CLIENT_SECRET'),
        username=os.environ.get('REDDIT_USERNAME') or os.environ.get('USERNAME'),
        password=os.environ.get('REDDIT_PASSWORD') or os.environ.get('PASSWORD'),
        user_agent='Cross-Sub Ban Bot/1.0'
    )
