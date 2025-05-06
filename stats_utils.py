def write_stats_sheet(sheet_cache, client, sheet_key):
    import gspread
    from datetime import datetime, timedelta

    try:
        stats_sheet = client.open_by_key(sheet_key).worksheet("Stats")
    except gspread.exceptions.WorksheetNotFound:
        stats_sheet = client.open_by_key(sheet_key).add_worksheet(title="Stats", rows="100", cols="10")

    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)
    daily_counts = {}
    weekly_counts = {}
    user_counts = {}

    for row in sheet_cache:
        ts_str = row.get("Timestamp", "")
        src = row.get("SourceSub", "unknown")
        actor = row.get("Mod", "unknown")
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except:
            continue

        date_key = ts.date().isoformat()
        src = src.strip() or "unknown"
        actor = actor.strip() or "unknown"

        daily_counts.setdefault(date_key, {}).setdefault(src, 0)
        daily_counts[date_key][src] += 1

        if ts.date() >= week_ago:
            weekly_counts.setdefault(src, 0)
            weekly_counts[src] += 1

        if actor.lower() not in ["", "unknown"]:
            user_counts.setdefault(actor, 0)
            user_counts[actor] += 1

    # Clear and start from the top
    stats_sheet.clear()
    values = []

    # 📅 Daily Ban Count
    values.append(["📅 Daily Ban Count"])
    for day in sorted(daily_counts.keys(), reverse=True):
        for sub, count in daily_counts[day].items():
            values.append([day, sub, count])

    # 📈 Weekly Bans Per Subreddit
    values.append([])
    values.append(["📈 Weekly Bans Per Subreddit"])
    for sub, count in sorted(weekly_counts.items(), key=lambda x: -x[1]):
        values.append([sub, count])

    # 🏆 Top Banning Moderators
    values.append([])
    values.append(["🏆 Top Banning Moderators"])
    for mod, count in sorted(user_counts.items(), key=lambda x: -x[1]):
        values.append([mod, count])

    stats_sheet.update("A1", values)
    print("[INFO] Stats written to 'Stats' worksheet.")
