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

# Sent once, via modmail, to a sub the moment it is auto-added to the
# pact (i.e. the moment the bot is made a moderator of an allowlisted
# NHL sub). Tracked in pending_subs.json under "welcomed" so it is
# never sent twice.
WELCOME_SUBJECT = "Welcome to the NHL Cross-Sub Ban Pact \U0001F3D2"

WELCOME_BODY = """\
Hey mods \u2014 your sub is now part of the **NHL Cross-Sub Ban Pact**, \
because this bot (u/{bot}) was added to your mod team. Here's everything \
you need to know. (This is the only time we'll message you unprompted.)

**What the pact does**

When any participating sub bans a user with the ban reason \
`Auto XSub Pact Ban`, the bot detects it and propagates that ban to \
every other sub in the pact. One ban protects all of us at once. The \
goal is deterrence: when trolls know a drive-by in a rival's sub costs \
them their home sub too, most of them just don't.

**How to use it (this is the whole thing):**

- **To share a ban:** ban the user as you normally would, but use the \
  exact ban reason `Auto XSub Pact Ban`. That's the trigger. Nothing \
  else changes about how you moderate.
- **Normal bans are untouched:** the bot ONLY acts on that one specific \
  reason. Every other ban you make stays local to your sub.
- **To reverse a pact ban:** just unban the user in your sub \
  (within a reasonable window) and the bot propagates the unban too.

**Commands (via modmail to your own sub, or DM to u/{bot}):**

- `/xsub help` \u2014 this guide, any time
- `/xsub status <user>` \u2014 is a user pact-banned, and from where
- `/xsub history <user>` \u2014 full ban/forgive history for a user
- `/xsub pardon <user>` \u2014 reverse a pact ban for a user network-wide
- `/xsub exempt <user>` \u2014 keep a specific user un-banned in YOUR sub \
  even if the pact bans them elsewhere (useful for your own regulars)

**You keep full control.** No loss of autonomy \u2014 you can pardon or \
exempt anyone at any time, and leave the pact whenever you like by \
removing the bot from your mod team.

**See the pact live:** \
https://grafana.shkn.ws/public-dashboards/e2c2f2bad9bf4804b9328f4e99b27498

Questions? Just reply to this modmail or message u/re-verse. \
Glad to have you in. \U0001F3D2
"""


def send_welcome(reddit, sub_name, bot_username, dry_run=False):
    """
    Modmail a newly-added sub the how-to guide. Best-effort: never
    raises (a failed welcome must not abort discovery). Returns True if
    a message was sent (or would be, in dry-run).
    """
    body = WELCOME_BODY.format(bot=bot_username)
    if dry_run:
        print(f"[DRY-RUN][WELCOME] would modmail r/{sub_name} the onboarding guide")
        return True
    try:
        # Subreddit.message() sends to the target sub's MODERATORS
        # (modmail) — the whole mod team sees it, which is what we want
        # for onboarding. (modmail.create() only messages a single named
        # user, so it is not usable here.)
        reddit.subreddit(sub_name).message(
            subject=WELCOME_SUBJECT,
            message=body,
        )
        print(f"[WELCOME] sent onboarding guide to r/{sub_name} mods")
        return True
    except Exception as e:
        print(f"[WELCOME-ERROR] could not modmail r/{sub_name}: {e}")
        return False


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


def accept_pending_invites(reddit, allowlist, notify_func=None, dry_run=False):
    """
    Scan the bot's inbox for pending moderator invites and auto-accept
    ONLY those from subreddits on the NHL allowlist. Non-allowlisted
    invites are left pending and reported to the owner (never silently
    accepted) — a mod invite from an unknown sub is a potential abuse
    vector, since a pact mod can originate network-wide bans.

    Returns a list of (sub_name, accepted_bool, reason) for logging.
    Best-effort: never raises.
    """
    allow_lc = {a.strip().lower() for a in allowlist}
    results = []
    try:
        inbox_items = list(reddit.inbox.all(limit=25))
    except Exception as e:
        print(f"[INVITE-ERROR] could not read inbox: {e}")
        return results

    for msg in inbox_items:
        subj = (getattr(msg, "subject", "") or "")
        if not subj.startswith("Invitation to moderate"):
            continue
        sub = getattr(msg, "subreddit", None)
        if sub is None:
            continue
        name = sub.display_name
        if name.lower() not in allow_lc:
            # Not an NHL sub on our allowlist — do NOT accept. Flag it.
            print(f"[INVITE-SKIP] r/{name} invited the bot but is NOT on "
                  f"the NHL allowlist — leaving pending, notifying owner.")
            if notify_func:
                try:
                    notify_func(
                        f"\u26a0\ufe0f Mod invite from r/{name}, which is NOT on the "
                        f"NHL allowlist. Left it PENDING (not auto-accepted). "
                        f"If this is a legit new NHL sub, add it to "
                        f"nhl_allowlist.txt and it'll be accepted next run; "
                        f"otherwise ignore/decline it."
                    )
                except Exception:
                    pass
            results.append((name, False, "not_on_allowlist"))
            continue

        # On the allowlist — accept.
        if dry_run:
            print(f"[DRY-RUN][INVITE] would accept mod invite from r/{name}")
            results.append((name, True, "dry_run"))
            continue
        try:
            reddit.subreddit(name).mod.accept_invite()
            print(f"[INVITE-ACCEPT] accepted mod invite from r/{name} "
                  f"(on NHL allowlist)")
            results.append((name, True, "accepted"))
        except Exception as e:
            print(f"[INVITE-ERROR] failed to accept r/{name}: {e}")
            results.append((name, False, f"error: {e}"))

    return results


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
            # Welcome the new sub exactly once, via modmail. Tracked in
            # pending_state["welcomed"] so a re-run (or the sub briefly
            # dropping and re-adding the bot) never re-sends the guide.
            welcomed = pending_state.setdefault("welcomed", [])
            if name not in welcomed:
                if send_welcome(reddit, name, bot_username, dry_run=dry_run):
                    if not dry_run:
                        welcomed.append(name)
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
