def sync_bans_from_sub(sub_name):
    subreddit = reddit.subreddit(sub_name)
    MAX_LOG_AGE_MINUTES = 10

    for log in subreddit.mod.log(action='banuser', limit=50):
        user = log.target_author
        source_sub = f"r/{log.subreddit}"
        log_id = log.id
        timestamp = datetime.utcfromtimestamp(log.created_utc).strftime('%Y-%m-%d %H:%M:%S')
        description = log.description or ""

        # ✅ New: skip old modlog actions
        created_time = datetime.utcfromtimestamp(log.created_utc)
        if datetime.utcnow() - created_time > timedelta(minutes=MAX_LOG_AGE_MINUTES):
            print(f"[SKIP] Modlog too old for {user}, ignoring ID {log_id}")
            continue

        if description.strip().lower() != CROSS_SUB_BAN_REASON.lower():
            continue
        if source_sub not in TRUSTED_SOURCES:
            continue
        if user in EXEMPT_USERS or is_mod(subreddit, user):
            continue
        if already_logged_action(log_id):
            print(f"[SKIP] Already processed modlog ID {log_id}")
            continue
        if already_listed(user):
            print(f"[SKIP] User {user} already listed — skipping duplicate log")
            continue
        if get_recent_sheet_entries(source_sub) >= DAILY_BAN_LIMIT:
            print(f"[SKIP] {source_sub} hit daily limit for {user}")
            continue

        sheet.append_row([user, source_sub, timestamp, "", log_id])
        print(f"[LOGGED] {user} from {source_sub} — modlog ID: {log_id}")
