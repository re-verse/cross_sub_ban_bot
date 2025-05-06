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

    stats_sheet.clear()
    stats_sheet.update(values=[["\ud83d\udcc5 Daily Ban Count"]], range_name="A1")
    row = 2
    for day in sorted(daily_counts.keys(), reverse=True):
        for sub, count in daily_counts[day].items():
            stats_sheet.update(range_name=f"A{row}", values=[[day, sub, count]])
            row += 1

    row += 1
    stats_sheet.update(range_name=f"A{row}", values=[["\ud83d\udcc8 Weekly Bans Per Subreddit"]])
    row += 1
    for sub, count in sorted(weekly_counts.items(), key=lambda x: -x[1]):
        stats_sheet.update(range_name=f"A{row}", values=[[sub, count]])
        row += 1

    row += 1
    stats_sheet.update(range_name=f"A{row}", values=[["\ud83c\udfc6 Top Banning Moderators"]])
    row += 1
    for mod, count in sorted(user_counts.items(), key=lambda x: -x[1]):
        stats_sheet.update(range_name=f"A{row}", values=[[mod, count]])
        row += 1

    print("[INFO] Stats written to 'Stats' worksheet.")
