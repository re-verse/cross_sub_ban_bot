#!/usr/bin/env python3

import sys
import time
from datetime import datetime
from bot_config import reddit, TRUSTED_SUBS, CROSS_SUB_BAN_REASON
from log_utils import log_public_action

# --- AUTHORIZATION ---
AUTHORIZED_USER = "re-verse"

def usage():
    print("Usage: python3 super.py <ban|unban> u/username [reason]")
    sys.exit(1)

def main():
    if len(sys.argv) < 3:
        usage()

    action = sys.argv[1].lower()
    raw_user = sys.argv[2]
    reason = sys.argv[3] if len(sys.argv) > 3 else "Manual override"

    if not raw_user.startswith("u/"):
        print("[ERROR] Username must start with 'u/'")
        usage()

    username = raw_user[2:]

    print(f"== Superuser Action: {action.upper()} user u/{username} ==")
    print(f"[INFO] Reason: {reason}")
    print(f"[INFO] Acting on subs: {TRUSTED_SUBS}")
    print("")

    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
            if action == "ban":
                note = (
                    f"Superuser manual ban. Reason: {reason}"
                )
                sr.banned.add(username, ban_reason=CROSS_SUB_BAN_REASON, note=note)
                print(f"[BANNED] u/{username} in r/{sub}")
                log_public_action("BANNED", username, sub, "manual", f"{AUTHORIZED_USER} (super.py)", reason)

            elif action == "unban":
                sr.banned.remove(username)
                print(f"[UNBANNED] u/{username} in r/{sub}")
                log_public_action("UNBANNED", username, sub, "manual", f"{AUTHORIZED_USER} (super.py)", reason)

            else:
                print(f"[ERROR] Unknown action '{action}'")
                usage()

            time.sleep(2)  # Respect Reddit API rate limits

        except Exception as e:
            print(f"[ERROR] Failed in r/{sub} for u/{username}: {type(e).__name__}: {e}")

    print(f"== Superuser {action.upper()} complete for u/{username} ==")

if __name__ == "__main__":
    main()
