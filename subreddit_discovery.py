"""
Subreddit discovery + auto-onboarding.

Each cron tick, walks reddit.user.moderator_subreddits() to find subs
where the bot is a moderator, diffs against trusted_subs.txt, and:

- For new subs on nhl_allowlist.txt -> auto-add to trusted_subs.txt,
  DM the owner a one-line notification.
- For new subs NOT on the allowlist -> add to pending_subs.json, DM
  the owner once with permission details and approval instructions.
- For pending subs that have been waiting > 7 days -> re-notify once.

Permission validation: before adding any sub (allowlist or pending),
we check the bot has at minimum 'access' (modlog + modmail) and
'users' (ban authority), or 'all'. Subs with missing perms are still
recorded but flagged in the DM so the owner can chase the requesting
mod team to grant the missing scopes.

Removal isn't handled here — it's owner-initiated via /xsub remove.
(There is no automatic removal; persistently failing subs surface via
health_utils escalation alerts and the owner decides.)
"""
import json
import os
from datetime import datetime, timezone, timedelta

TRUSTED_SUBS_FILE = "trusted_subs.txt"
ALLOWLIST_FILE = "nhl_allowlist.txt"
PENDING_FILE = "pending_subs.json"

REQUIRED_PERMS = {"access", "users"}
ALL_PERMS = {"all"}
RENOTIFY_AFTER_DAYS = 7


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat(timespec="seconds")


def _read_lines(path):
    """Read a text file as a list of non-empty, non-comment lines (lowercased)."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s.lower())
    return out


def load_allowlist():
    return set(_read_lines(ALLOWLIST_FILE))


def load_trusted():
    """Return the current trusted-subs list (lowercased) preserving order."""
    if not os.path.exists(TRUSTED_SUBS_FILE):
        return []
    with open(TRUSTED_SUBS_FILE) as f:
        return [line.strip().lower() for line in f if line.strip()]


def save_trusted(subs):
    """Persist the trusted-subs list. Preserves order, one per line."""
    with open(TRUSTED_SUBS_FILE, "w") as f:
        for s in subs:
            f.write(f"{s}\n")


def load_pending():
    if not os.path.exists(PENDING_FILE):
        return {"pending": {}}
    try:
        with open(PENDING_FILE) as f:
            data = json.load(f)
        data.setdefault("pending", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"pending": {}}


def save_pending(state):
    try:
        with open(PENDING_FILE, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except OSError as e:
        print(f"[DISCOVERY-ERROR] Could not write {PENDING_FILE}: {e}")


def _bot_permissions(subreddit, bot_name):
    """
    Return (granted_perms, has_required) for the bot in this sub.
    granted_perms is a set of strings; has_required is bool.

    PRAW's subreddit.moderator(redditor=name) returns a list (length 0 or 1)
    of Redditor objects with a mod_permissions attribute. 'all' is special
    — it implies every permission.
    """
    try:
        mods = list(subreddit.moderator(redditor=bot_name))
    except Exception as e:
        print(f"[DISCOVERY-WARN] Could not read moderator perms for r/{subreddit.display_name}: {e}")
        return set(), False
    if not mods:
        return set(), False
    perms = set(p.lower() for p in (mods[0].mod_permissions or []))
    has_required = bool(ALL_PERMS & perms) or REQUIRED_PERMS.issubset(perms)
    return perms, has_required


def discover(reddit, bot_username, trusted, allowlist, pending_state,
             notify_func, dry_run=False):
    """
    Walk moderator_subreddits, diff against trusted, and act.

    reddit: live PRAW Reddit instance
    bot_username: lowercased bot account name (e.g. 'xsub-pact-bot')
    trusted: list[str] of currently-trusted sub names (lowercased)
    allowlist: set[str] of NHL team sub names (lowercased)
    pending_state: dict from load_pending(); mutated in place
    notify_func: callable(subject, body, dry_run) -> bool, used to DM owner
    dry_run: if True, log intent but do not modify trusted_subs.txt or
        pending_subs.json or send DMs.

    Returns the (possibly mutated) trusted list. Caller should compare
    to the input and save_trusted() if changed.
    """
    print("[DISCOVERY] Walking moderator_subreddits...")
    trusted_set = set(s.lower() for s in trusted)
    try:
        modded_subs = list(reddit.user.moderator_subreddits(limit=None))
    except Exception as e:
        print(f"[DISCOVERY-ERROR] Could not list moderator_subreddits: {e}")
        return trusted

    seen = set()
    new_auto_added = []
    new_pending = []
    perm_issues = []

    # Cleanup pass: prune the pending queue of entries that no longer
    # belong there. Two cases:
    # 1. Sub is now in trusted (manual edit to trusted_subs.txt, or
    #    a previous /xsub approve that didn't fully clean up).
    # 2. Sub is a profile sub (u_<username>) — every Reddit user is
    #    "mod" of their own profile sub, including the bot. These
    #    can never be ban-propagation participants by design.
    pending = pending_state.get("pending", {})
    for stale in list(pending.keys()):
        if stale in trusted_set or stale.startswith("u_"):
            pending.pop(stale)

    for sr in modded_subs:
        name = sr.display_name.lower()
        # Skip the bot's own user-profile sub before any other work.
        # Same reason as the cleanup pass above: these are not real
        # participating subs.
        if name.startswith("u_"):
            continue
        seen.add(name)
        if name in trusted_set:
            continue  # already participating

        perms, has_required = _bot_permissions(sr, bot_username)

        if name in allowlist:
            if not has_required:
                perm_issues.append((name, sorted(perms)))
                # Still add — they're on the allowlist, but flag for owner
            if not dry_run:
                trusted.append(name)
                trusted_set.add(name)
            new_auto_added.append((name, sorted(perms), has_required))
            # Clean from pending if it was there
            pending_state.get("pending", {}).pop(name, None)
            continue

        # Non-allowlist sub. Add to pending if not already there.
        pending = pending_state.setdefault("pending", {})
        if name not in pending:
            pending[name] = {
                "first_seen": _now_iso(),
                "permissions": sorted(perms),
                "has_required_perms": has_required,
                "notified": False,
            }
            new_pending.append(name)
        else:
            # Update permissions in case they've changed since first seen
            pending[name]["permissions"] = sorted(perms)
            pending[name]["has_required_perms"] = has_required

    # Re-notify pending subs older than RENOTIFY_AFTER_DAYS
    stale_pending = []
    cutoff = _now() - timedelta(days=RENOTIFY_AFTER_DAYS)
    for name, info in pending_state.get("pending", {}).items():
        if name in new_pending:
            continue
        first = _parse_iso(info.get("first_seen"))
        last_notified = _parse_iso(info.get("last_notified_at"))
        if first and first < cutoff:
            ref = last_notified or first
            if ref < cutoff:
                stale_pending.append(name)

    # Dispatch a single consolidated DM if anything happened
    if new_auto_added or new_pending or stale_pending or perm_issues:
        subject, body = _format_discovery_alert(
            new_auto_added, new_pending, stale_pending, perm_issues,
        )
        if notify_func(subject, body, dry_run=dry_run):
            if not dry_run:
                stamp = _now_iso()
                for name in new_pending + stale_pending:
                    if name in pending_state.get("pending", {}):
                        pending_state["pending"][name]["notified"] = True
                        pending_state["pending"][name]["last_notified_at"] = stamp

    print(
        f"[DISCOVERY] done: {len(modded_subs)} subs modded, "
        f"{len(new_auto_added)} auto-added, {len(new_pending)} new pending, "
        f"{len(stale_pending)} stale pending, {len(perm_issues)} perm issues"
    )
    return trusted


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _format_discovery_alert(auto_added, pending_new, pending_stale, perm_issues):
    """Build (subject, body) for the consolidated discovery DM."""
    parts = []
    sections = []

    if auto_added:
        sections.append("auto-added")
        parts.append(f"**Auto-added ({len(auto_added)}):**")
        for name, perms, ok in auto_added:
            flag = "" if ok else "  ⚠️ missing required perms"
            perm_str = ", ".join(perms) if perms else "(none reported)"
            parts.append(f"- r/{name} — perms: {perm_str}{flag}")
        parts.append("")

    if pending_new:
        sections.append(f"{len(pending_new)} pending")
        parts.append(f"**New pending — not on NHL allowlist ({len(pending_new)}):**")
        for name in pending_new:
            parts.append(f"- r/{name}")
        parts.append("Reply `/xsub approve r/<name>` to add, "
                     "or `/xsub decline r/<name>` to dismiss.")
        parts.append("")

    if pending_stale:
        sections.append(f"{len(pending_stale)} stale")
        parts.append(f"**Pending > 7 days, re-notifying ({len(pending_stale)}):**")
        for name in pending_stale:
            parts.append(f"- r/{name}")
        parts.append("")

    if perm_issues:
        sections.append("perm issues")
        parts.append(f"**Permission issues ({len(perm_issues)}):**")
        for name, perms in perm_issues:
            perm_str = ", ".join(perms) if perms else "(none)"
            parts.append(f"- r/{name} has perms [{perm_str}]; "
                         f"need 'access' and 'users' (or 'all')")
        parts.append("")

    summary = " / ".join(sections) if sections else "no new activity"
    subject = f"[xsub-pact-bot] discovery: {summary}"
    body = "Sub-discovery report:\n\n" + "\n".join(parts)
    return subject, body


def approve_sub(name, trusted, pending_state):
    """Move a pending sub to trusted. Returns (success, message)."""
    name = name.lower().lstrip("r/").strip("/")
    if not name:
        return False, "no sub name"
    if name in (s.lower() for s in trusted):
        return False, f"r/{name} is already trusted"
    pending = pending_state.get("pending", {})
    if name not in pending:
        # Allow approving a sub that hasn't been seen yet (advance approval)
        trusted.append(name)
        return True, f"r/{name} added to trusted list (not previously pending)"
    pending.pop(name)
    trusted.append(name)
    return True, f"r/{name} approved and added to trusted list"


def decline_sub(name, pending_state):
    """Remove a pending sub from the queue without trusting. Idempotent."""
    name = name.lower().lstrip("r/").strip("/")
    if not name:
        return False, "no sub name"
    pending = pending_state.get("pending", {})
    if name not in pending:
        return False, f"r/{name} not in pending queue"
    pending.pop(name)
    return True, f"r/{name} declined and removed from pending"


def remove_sub(name, trusted, pending_state=None):
    """Remove a sub from the trusted list. Also drops any pending entry."""
    name = name.lower().lstrip("r/").strip("/")
    if not name:
        return False, "no sub name"
    if name not in (s.lower() for s in trusted):
        return False, f"r/{name} is not in the trusted list"
    trusted[:] = [s for s in trusted if s.lower() != name]
    if pending_state is not None:
        pending_state.get("pending", {}).pop(name, None)
    return True, f"r/{name} removed from trusted list"
