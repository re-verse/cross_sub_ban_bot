def is_mod(subreddit, user):
    """
    Check if a given user is a moderator of the given subreddit.
    """
    try:
        mod_list = {m.name.lower() for m in subreddit.moderator()}
        return user.lower() in mod_list
    except Exception:
        return False
