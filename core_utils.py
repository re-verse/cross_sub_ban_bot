def is_mod(subreddit, user):
    """
    Check if a given user is a moderator of the given subreddit.
    """
    try:
        mod_list = {m.name.lower() for m in subreddit.moderator()}
        return user.lower() in mod_list
    except Exception:
        return False

def is_forgiven(user, sheet_cache):
    for r in sheet_cache:
        if r.get('Username','').lower() == user.lower() and str(r.get('ManualOverride','')).lower() in ('yes','true'):
            return True
    return False

def exempt_subs_for_user(user, sheet_cache):
    for r in sheet_cache:
        if r.get('Username','').lower() == user.lower():
            field = str(r.get('ExemptSubs','')).lower()
            if field:
                return {sub.strip() for sub in field.split(',') if sub.strip()}
    return set()

def get_recent_sheet_entries(source_sub, sheet_cache):
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=1)
    count = 0
    for r in sheet_cache:
        if r.get('SourceSub') == source_sub:
            ts = r.get('Timestamp')
            try:
                t = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
                if t > cutoff:
                    count += 1
            except:
                pass
    return count
