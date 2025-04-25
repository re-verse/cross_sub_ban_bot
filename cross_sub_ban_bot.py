# inside the enforce_bans_on_sub loop...
    for row in rows:
        try:
            user = row.get('Username', '').strip()
            source_sub = row.get('SourceSub', '').strip()
            if not user or not source_sub:
                raise ValueError("Missing Username or SourceSub")
        except Exception as e:
            print(f"[ERROR] Skipping row due to missing or invalid data: {e}")
            continue

        try:
            user_lower = user.lower()
            already_banned = user_lower in current_bans
            is_mod_user = is_mod(subreddit, user)
            is_exempt = user_lower in EXEMPT_USERS
            is_override = is_forgiven(user)

            if is_override:
                if already_banned:
                    try:
                        ban_obj = current_bans[user_lower]
                        ban_reason_text = getattr(ban_obj, "note", "") or ""
                        if CROSS_SUB_BAN_REASON.lower() in ban_reason_text.lower():
                            subreddit.banned.remove(user)
                            print(f"[UNBANNED] {user} in {sub_name} (forgiven and ban matched reason)")
                            log_public_action("UNBANNED", user, sub_name, source_sub, "auto", "Forgiven")
                    except Exception as e:
                        print(f"[ERROR] Failed to unban {user} in {sub_name}: {e}")
                continue

            if not already_banned:
                if str(row.get('ManualOverride', '')).strip().lower() not in {'yes', 'true'}:
                    print(f"[NOTICE] {user} was manually unbanned in {sub_name} without override")
                    try:
                        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                        sheet.append_row([user, source_sub, "Manual unban detected", now, "", "", "", sub_name, now])
                    except Exception as e:
                        print(f"[ERROR] Failed to log manual unban for {user}: {e}")
                continue

            if already_banned or is_exempt or is_mod_user:
                continue

            try:
                subreddit.banned.add(
                    user,
                    ban_reason=CROSS_SUB_BAN_REASON,
                    note=f"Cross-sub ban from {source_sub}"
                )
                print(f"[BANNED] {user} in {sub_name}")
                log_public_action("BANNED", user, sub_name, source_sub, "auto", "")
            except Exception as e:
                print(f"[ERROR] Failed to ban {user} in {sub_name}: {e}")

        except Exception as e:
            print(f"[ERROR] Unexpected error processing user {row.get('Username', '<unknown>')}: {e}")
