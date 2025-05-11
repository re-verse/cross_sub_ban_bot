def check_superuser_command():
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
                print("[SUPER] Invalid format. Use: /xsub super <ban|unban> u/username [reason...]")
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
