from datetime import datetime
import time
from log_utils import log_public_action

def check_superuser_command():
    from bot_config import reddit, CROSS_SUB_BAN_REASON, TRUSTED_SUBS
    from cross_sub_ban_bot import SHEET_CACHE
    try:
        inbox = reddit.inbox.unread(limit=None)
        for item in inbox:
            if not hasattr(item, 'author') or item.author is None:
                continue
            author = str(item.author.name).lower()
            if author != "re-verse":
                continue  # only accept from you

            body = item.body.strip()
            if not body.lower().startswith("/xsub super"):
                continue

            print(f"[SUPER] Received superuser command from u/{author}: {body}")
            tokens = body.split()
            if len(tokens) < 4:
                print("[SUPER] Invalid format. Use: /xsub super <ban|unban|status> u/username [reason...]")
                item.mark_read()
                continue

            action = tokens[2].lower()
            raw_user = tokens[3]
            reason = " ".join(tokens[4:]) if len(tokens) > 4 else "Superuser override"

            if not raw_user.startswith("u/"):
                print("[SUPER] Username must start with 'u/'")
                item.mark_read()
                continue

            username = raw_user[2:]

            if action == "status":
                handle_status_command(username)
                item.reply(f"✅ Status report for u/{username} sent via Reddit DM.")
                item.mark_read()
                continue

            for sub in TRUSTED_SUBS:
                try:
                    sr = reddit.subreddit(sub)
                    if action == "ban":
                        note = f"Superuser manual ban. Reason: {reason}"
                        sr.banned.add(username, ban_reason=CROSS_SUB_BAN_REASON, note=note)
                        print(f"[BANNED] u/{username} in r/{sub} by superuser")
                        log_public_action("BANNED", username, sub, "manual", f"re-verse (supermodmail)", reason)
                    elif action == "unban":
                        sr.banned.remove(username)
                        print(f"[UNBANNED] u/{username} in r/{sub} by superuser")
                        log_public_action("UNBANNED", username, sub, "manual", f"re-verse (supermodmail)", reason)
                    time.sleep(2)
                except Exception as e:
                    print(f"[ERROR] Failed to {action} u/{username} in r/{sub}: {e}")

            item.reply(f"✅ Action complete: {action.upper()} u/{username} in all participating subs.")
            item.mark_read()

    except Exception as e:
        print(f"[ERROR] In superuser command handler: {e}")

def handle_status_command(username):
    from bot_config import reddit, TRUSTED_SUBS
    from cross_sub_ban_bot import SHEET_CACHE
    username_lc = username.lower()
    subs_banned_in = []
    last_action = None

    # Find sheet row
    sheet_rows = [
        row for row in SHEET_CACHE
        if row.get("Username", "").strip().lower() == username_lc
    ]
    if sheet_rows:
        row = sheet_rows[0]
        source_sub = row.get("SourceSub", "❓")
        forgiven = bool(row.get("ForgiveTimestamp", "").strip())
        exemptions = row.get("ExemptSubs", "").strip()
    else:
        source_sub = "No entry"
        forgiven = False
        exemptions = ""

    # Live ban check
    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
            if username_lc in [b.name.lower() for b in sr.banned(limit=100)]:
                subs_banned_in.append(sub)
        except Exception:
            continue

    # Check last modlog entry
    for sub in TRUSTED_SUBS:
        try:
            for log in reddit.subreddit(sub).mod.log(limit=50):
                if getattr(log, "target_author", "").lower() == username_lc:
                    last_action = f"{log.action} in r/{sub} by u/{log.mod} on {datetime.utcfromtimestamp(log.created_utc).strftime('%Y-%m-%d')}"
                    break
        except Exception:
            continue
        if last_action:
            break

    # Assemble message
    lines = [f"Status report for u/{username}:"]
    lines.append(f"🧾 Sheet Entry: {'Yes' if sheet_rows else 'No'} (origin: {source_sub})")
    lines.append(f"⛔ Currently Banned In: {', '.join(subs_banned_in) or 'None'}")
    lines.append(f"✅ Forgiven: {'Yes' if forgiven else 'No'}")
    lines.append(f"✳️ Exempt in: {exemptions or 'None'}")
    lines.append(f"🗑️ Last ModLog Action: {last_action or 'None found'}")

    reddit.redditor("re-verse").message("User Status", "\n".join(lines))
