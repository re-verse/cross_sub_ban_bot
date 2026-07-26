"""
DM inbox listener for help requests.

Modmail handles the four mutating commands (status, pardon, exempt) and
help, but a non-trivial fraction of mods will reflexively PM the bot
account directly instead of going through their own sub's modmail. This
module catches that path and auto-replies with the help text, gated on
the sender being a moderator of at least one trusted sub.

Strictly read-only for everyone except the bot owner:
- /xsub help (and unknown /xsub commands) get a reply
- Mutating sub-action commands (pardon/exempt/super) are deliberately
  not supported over DM. Asking a mod to use modmail keeps every
  action attributable to a specific sub's mod team, which matters
  for audit + abuse cases.
- Owner-only network management commands DO work over DM, since
  they're cross-network (approve a new sub, decline, remove from
  trusted list) rather than sub-specific. Owner is identified by
  OWNER_USERNAME in config.
- Non-mods get no reply at all (no engagement signal to drive-bys).
"""
import os
import re
import prawcore
from bot_config import reddit, TRUSTED_SUBS, OWNER_USERNAME
from core_utils import is_mod
from subreddit_discovery import (
    load_trusted, save_trusted, load_pending, save_pending,
    approve_sub, decline_sub, remove_sub,
)

# Cap inbox scan per run. Reddit's unread inbox shouldn't be large for a
# bot account, but defensively bound it so a flood can't stall a cron tick.
_MAX_INBOX_ITEMS = 50

_HELP_TEXT = (
    "**Cross-Sub Ban Pact Bot — Help**\n\n"
    "I can take commands via either:\n"
    "1. **Modmail to your sub** — required for any sub-specific action "
    "(status, history, pardon, exempt, super ban), so the action is "
    "attributable to a specific mod team.\n"
    "2. **Direct message to me** — `/xsub help` works for any trusted-sub "
    "mod; the owner-only network-management commands (approve/decline/"
    "remove) also work here.\n\n"
    "    /xsub help                       — this message\n"
    "    /xsub status u/username          — show this user's status across the network\n"
    "    /xsub history u/username         — chronological audit trail (modmail only)\n"
    "    /xsub pardon u/username          — forgive + unban a user (origin-sub mods only, modmail only)\n"
    "    /xsub exempt u/username          — exempt user from bans in your sub only (modmail only)\n"
    "    /xsub super ban u/username ...   — manual cross-sub ban (bot owner only, modmail only)\n"
    "    /xsub approve r/subname          — move sub from pending to trusted (owner only, DM or modmail)\n"
    "    /xsub decline r/subname          — drop pending sub without trusting (owner only)\n"
    "    /xsub remove r/subname           — remove sub from trusted list (owner only)\n\n"
    "The pact triggers on bans whose reason is exactly "
    "**Auto XSub Pact Ban**.\n\n"
    "Public log: https://re-verse.github.io/cross_sub_ban_bot/public_ban_log.html"
)


def _bot_username():
    """Return the bot's own Reddit username (lowercased)."""
    name = os.environ.get("REDDIT_USERNAME", "")
    if name:
        return name.lower()
    try:
        return reddit.user.me().name.lower()
    except Exception:
        return ""


def _is_trusted_mod(sender):
    """
    True if `sender` is a mod of at least one trusted sub.

    Iterates trusted subs in order and returns on first match. For a mod
    of an early-listed sub this is cheap. For a non-mod it walks the
    full list, which is the worst case (~9 API calls). That's fine at
    20-min cadence with the inbox cap above.
    """
    for sub in TRUSTED_SUBS:
        try:
            sr = reddit.subreddit(sub)
            if is_mod(sr, sender):
                return True
        except Exception:
            continue
    return False


def _mark_read(item, dry_run):
    """Mark an inbox item read, honoring dry_run. Never raises."""
    if dry_run:
        return
    try:
        item.mark_read()
    except Exception:
        pass


def _try_accept_mod_invite(item, dry_run):
    """
    Auto-accept a moderator invite, but ONLY for subs on the NHL
    allowlist. Returns True if the item was an invite (handled), so the
    caller can skip further processing of it.

    Safety: an invite from a sub NOT on the allowlist is deliberately
    left alone (not accepted, not marked read) so a human can review it.
    We never want the bot silently gaining mod powers somewhere
    unexpected just because someone invited it.
    """
    subject = (getattr(item, "subject", "") or "").lower()
    # Reddit's invite subject is "invitation to moderate /r/<sub>"
    if "invitation to moderate" not in subject:
        return False

    sub_obj = getattr(item, "subreddit", None)
    sub_name = getattr(sub_obj, "display_name", None) or ""
    if not sub_name:
        m = re.search(r"/r/([A-Za-z0-9_]+)", getattr(item, "subject", "") or "")
        sub_name = m.group(1) if m else ""
    if not sub_name:
        print("[INVITE] Could not determine subreddit from invite; leaving unread.")
        return True

    try:
        import subreddit_discovery
        allowlist = {a.lower() for a in subreddit_discovery.load_allowlist()}
    except Exception as e:
        print(f"[INVITE-ERROR] Could not load allowlist: {e}; leaving invite unread.")
        return True

    if sub_name.lower() not in allowlist:
        print(f"[INVITE] r/{sub_name} is NOT on the NHL allowlist — "
              f"leaving invite for manual review (not accepting).")
        return True

    if dry_run:
        print(f"[DRY-RUN][INVITE] would accept mod invite for r/{sub_name} "
              f"(on allowlist)")
        return True

    try:
        reddit.subreddit(sub_name).mod.accept_invite()
        print(f"[INVITE] Accepted moderator invite for r/{sub_name} "
              f"(on NHL allowlist). Discovery will onboard it and send "
              f"the welcome guide.")
        _mark_read(item, dry_run)
    except Exception as e:
        print(f"[INVITE-ERROR] Failed to accept invite for r/{sub_name}: {e}")
    return True


def check_dm_inbox(dry_run=False):
    """
    Read unread DMs to the bot and respond to /xsub help from trusted mods.

    dry_run: if True, log intent but don't reply or mark anything read.
    """
    print("[DM-INBOX] Checking for /xsub help DMs...")
    bot_name = _bot_username()
    handled = 0
    skipped = 0

    try:
        items = list(reddit.inbox.unread(limit=_MAX_INBOX_ITEMS))
    except Exception as e:
        print(f"[DM-INBOX-ERROR] Could not read inbox: {e}")
        return

    for item in items:
        # Skip modmail items (they have a different shape and are handled
        # by modmail_utils). Reddit puts modmail in the same inbox stream
        # but flags it via the `was_comment` / subject pattern.
        try:
            kind = getattr(item, "kind", "") or item.__class__.__name__.lower()
            if "comment" in kind:
                # Comment replies / username mentions — not our path.
                # Mark read so they don't clog the unread window: we only
                # scan the first _MAX_INBOX_ITEMS unread items, so any
                # item left unread forever eventually starves real
                # commands out of the scan.
                _mark_read(item, dry_run)
                continue
        except Exception:
            pass

        # Moderator invites come from Reddit itself (no author). Handle
        # them BEFORE the author check, or they'd be silently discarded.
        if _try_accept_mod_invite(item, dry_run):
            continue

        author_obj = getattr(item, "author", None)
        if author_obj is None:
            _mark_read(item, dry_run)
            continue
        sender = (getattr(author_obj, "name", "") or "").lower()
        if not sender or sender == bot_name:
            _mark_read(item, dry_run)
            continue

        body = (getattr(item, "body", "") or "").strip()
        if not body:
            _mark_read(item, dry_run)
            continue

        body_l = body.lower().lstrip("> ").strip()
        if not body_l.startswith("/xsub"):
            # Not a command DM — don't reply (the bot account isn't a
            # help desk for arbitrary user questions), but DO mark read
            # to keep the unread window clear for real commands.
            _mark_read(item, dry_run)
            continue

        # Gate on mod status before doing anything else
        if not _is_trusted_mod(sender):
            print(f"[DM-INBOX-SKIP] u/{sender} sent /xsub but is not a trusted-sub mod")
            skipped += 1
            if not dry_run:
                try:
                    item.mark_read()
                except Exception:
                    pass
            continue

        parts = body_l.split()
        cmd = parts[1] if len(parts) >= 2 else ""

        if cmd == "help":
            reply_body = _HELP_TEXT
            log_tag = "help"
        elif cmd in ("approve", "decline", "remove"):
            reply_body, log_tag = _handle_management_cmd(cmd, parts, sender, dry_run)
        elif cmd in ("status", "history", "pardon", "exempt", "super"):
            reply_body = (
                f"ℹ️ `/xsub {cmd}` is only available via modmail to your own sub, "
                "not via DM. This keeps every action attributable to a specific "
                "mod team. Send the command as modmail from your sub instead.\n\n"
                "Send `/xsub help` here any time for the full command list."
            )
            log_tag = f"{cmd}-redirect"
        else:
            reply_body = (
                f"⚠️ Unknown command `/xsub {cmd or '(empty)'}`. "
                "Send `/xsub help` for the command list."
            )
            log_tag = f"unknown:{cmd or 'empty'}"

        if dry_run:
            print(f"[DRY-RUN][DM-INBOX-REPLY] u/{sender} tag={log_tag}: would reply")
            continue

        try:
            item.reply(reply_body)
            item.mark_read()
            print(f"[DM-INBOX-REPLY] u/{sender} tag={log_tag}: replied")
            handled += 1
        except Exception as e:
            print(f"[DM-INBOX-ERROR] reply to u/{sender} failed: {e}")

    if handled or skipped:
        print(f"[DM-INBOX] done: {handled} replied, {skipped} non-mod skipped")


def _handle_management_cmd(cmd, parts, sender, dry_run):
    """
    Owner-only DM commands for managing the trusted-subs list.

    /xsub approve r/SubName  -> move from pending to trusted
    /xsub decline r/SubName  -> drop from pending without trusting
    /xsub remove  r/SubName  -> remove from trusted list

    Returns (reply_body, log_tag) for the caller to dispatch.
    """
    if sender != OWNER_USERNAME.lower():
        return (
            f"❌ `/xsub {cmd}` is restricted to u/{OWNER_USERNAME}.",
            f"{cmd}-unauthorized",
        )
    if len(parts) < 3:
        return (
            f"⚠️ Format: `/xsub {cmd} r/<subname>`",
            f"{cmd}-no-arg",
        )

    target = parts[2]

    if dry_run:
        return (
            f"🧪 (dry-run) would `/xsub {cmd} {target}` — no state changed.",
            f"{cmd}-dry-run",
        )

    try:
        trusted = load_trusted()
        pending = load_pending()
        if cmd == "approve":
            ok, msg = approve_sub(target, trusted, pending)
        elif cmd == "decline":
            ok, msg = decline_sub(target, pending)
        elif cmd == "remove":
            ok, msg = remove_sub(target, trusted, pending)
        else:
            return (f"⚠️ Unknown management cmd `{cmd}`.", f"{cmd}-unknown")

        if ok:
            save_trusted(trusted)
            save_pending(pending)
            return (
                f"✅ {msg}. Takes effect on the next cron tick (~20 min).",
                f"{cmd}-ok",
            )
        else:
            return (f"⚠️ {msg}.", f"{cmd}-fail")
    except Exception as e:
        print(f"[MGMT-CMD-ERROR] cmd={cmd} target={target}: {e}")
        return (f"❌ Internal error processing `/xsub {cmd} {target}` — see bot logs.",
                f"{cmd}-error")
